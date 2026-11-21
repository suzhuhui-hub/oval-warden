"""Oval Warden, a survival-first entry for the Builders Buildathon."""

import math
from time import perf_counter

from arena import Arena
from combat import angle_delta, bearing, clamp, direction, fire_control, target_options, unit
import mechfight


class Bot:
    name = "Oval Warden"

    def __init__(self, hello):
        self.config = hello["config"]
        self.arena = Arena(hello["map"]["dir"], self.config["mech"])
        self.route = []
        self.route_goal = None
        self.replan = 0
        self.last_position = None
        self.last_command = (0, 0)
        self.stuck = 0
        self.last_attacker = None
        self.last_damage = -100
        self.last_target = None
        self.last_goal = None
        self.last_take = -100
        self.objects = {}
        self.jet_until = 0
        self.loot_radius = self.config["loot"]["pickup_radius"]
        self.decide_s = self.config["match"]["decide_every"] / self.config["match"]["tick_hz"]

    def safe(self, point, me, obs):
        zone = obs["zone"]
        # Forecast the circle when we arrive, including a modest detour allowance.
        t = obs["t"]
        eta = math.dist(me["pos"][:2], point[:2]) / me["stats"]["walk_speed"] * 1.20
        fraction = clamp((t + eta + 5 - max(t, zone["shrink_start"])) /
                         max(1, zone["shrink_end"] - max(t, zone["shrink_start"])), 0, 1)
        center = [a + (b - a) * fraction for a, b in zip(zone["center"], zone["next_center"])]
        radius = zone["radius"] + (zone["next_radius"] - zone["radius"]) * fraction
        return radius - math.dist(point[:2], center)

    def update(self, me, obs):
        t = obs["t"]
        xy = me["pos"][:2]
        if self.last_position is not None:
            wants_motion = math.hypot(*self.last_command) > 0.75
            progress = math.dist(xy, self.last_position)
            self.stuck = self.stuck + 1 if wants_motion and progress < 0.38 else max(0, self.stuck - 2)
            if self.stuck == 8:
                self.replan = 0
        self.last_position = xy
        for event in obs["events"]:
            if event["type"] == "damaged" and event.get("bearing") is not None:
                self.last_attacker = (event.get("by"), event["bearing"])
                self.last_damage = t
        for key in ("pickups", "cubes", "crates"):
            for item in obs[key]:
                self.objects[item["id"]] = (key, item, t)
        live_ids = {item["id"] for key in ("pickups", "cubes", "crates") for item in obs[key]}
        for item_id, (_, item, seen) in list(self.objects.items()):
            if t - seen > 7 or (item_id not in live_ids and math.dist(me["pos"], item["pos"]) < 13):
                del self.objects[item_id]

    def assess_threats(self, me, obs):
        threats = []
        now = obs["t"]
        for enemy in obs["mechs"]:
            if enemy["kind"] != "mech":
                continue
            distance = math.dist(me["pos"], enemy["pos"])
            if distance > 185:
                continue
            aiming_at_us = abs(angle_delta(bearing(enemy["pos"], me["pos"]), enemy["aim"][0])) < 0.63
            attacker = self.last_attacker and self.last_attacker[0] == enemy["id"] and now - self.last_damage < 3
            close = clamp((115 - distance) / 110, 0, 1)
            weight = 0.15 + close + (0.73 if attacker else 0) + (0.35 if aiming_at_us else 0)
            if "lightning" in enemy.get("weapons", ()) and distance < 60:
                weight += 0.45
            if enemy.get("charging") and aiming_at_us:
                weight += 0.45
            threats.append((weight, distance, enemy, aiming_at_us or attacker))
        threats.sort(key=lambda data: -data[0])
        return threats

    def weapon_priority(self, me, obs, kind):
        radius = obs["zone"]["next_radius"]
        if kind == "laser":
            return 5.8
        if kind == "mortar":
            return 4.45 if radius > 65 else 2.8
        if kind == "lightning":
            return 4.75 if radius <= 70 else 3.45
        return 3.9 if me["cubes"] >= 3 or radius > 70 else 3.6

    def pickup_value(self, me, obs, kind):
        held = me["weapons"]
        if any(w and kind == w["kind"] for w in held):
            return None
        if any(w is None for w in held):
            return (self.weapon_priority(me, obs, kind), None) if kind != "gun" else None
        eligible = [(self.weapon_priority(me, obs, w["kind"]), i) for i, w in enumerate(held)
                    if not w.get("charge", 0)]
        if not eligible:
            return None
        current, index = min(eligible)
        value = self.weapon_priority(me, obs, kind)
        # Keep a reliable projectile weapon until the stronger second slot is available.
        if value < current + 0.65:
            return None
        return value, index

    def pick_actions(self, action, me, obs):
        offer = obs.get("offer")
        if offer:
            ratings = {
                "plating": 9.8, "nanites": 9.5 if obs["t"] < 315 else 6.5,
                "armor": 9.2, "servos": 8.4, "jets": 8.1, "laser_damage": 8.1,
                "gun_mag": 7.9, "mortar_reload": 7.7, "lightning_efficiency": 7.7,
                "lightning_range": 7.65, "laser_charge": 7.6, "gun_damage": 7.2,
                "gun_rate": 7.1, "reactor": 7.0, "capacitor": 6.4, "gyros": 6.3,
                "mortar_splash": 6.3, "mortar_velocity": 6.25, "gun_velocity": 6.0,
                "sensors": 5.7, "laser_pierce": 4.0, "lightning_chain": 3.8,
            }
            action["pick"] = max(enumerate(offer["choices"]), key=lambda p: ratings.get(p[1]["id"], 0))[0]

        if obs["t"] - self.last_take < 0.25:
            return
        choices = []
        for item in obs["pickups"]:
            if math.dist(me["pos"][:2], item["pos"][:2]) > self.loot_radius or abs(me["pos"][2] - item["pos"][2]) > 4:
                continue
            value = self.pickup_value(me, obs, item["kind"])
            if value is not None:
                choices.append((value[0], item["id"], value[1]))
        if choices:
            _, action["take"], discard = max(choices)
            if discard is not None:
                action["drop"] = discard
            self.last_take = obs["t"]

    def goal(self, me, obs, threats, retreat):
        xy = me["pos"][:2]
        zone = obs["zone"]
        now = obs["t"]
        ahead = zone["next_center"]
        next_dist = math.dist(xy, ahead)
        eta = next_dist / me["stats"]["walk_speed"] * 1.28
        outside = math.dist(xy, zone["center"]) - zone["radius"]
        urgent = outside > -9 or (
            next_dist > zone["next_radius"] - 8 and now + eta + 10 > zone["shrink_end"]
        )
        if zone["next_radius"] < 3 or zone["radius"] < 29:
            return "endgame", tuple(ahead), True, None
        # Aim for the near side of the next circle, leaving room to dodge.
        edge = max(0.0, min(zone["next_radius"] - 25, zone["next_radius"] * 0.63))
        vector = unit(xy[0] - ahead[0], xy[1] - ahead[1])
        fallback = (ahead[0] + edge * vector[0], ahead[1] + edge * vector[1])
        if urgent:
            return "zone", fallback, urgent, None

        candidates = []
        hp = me["hp"] / me["max_hp"]
        for drop in obs["drops"]:
            x, y, _ = drop["pos"]
            distance = math.dist(xy, (x, y))
            arrive = distance / me["stats"]["walk_speed"] * 1.20
            wait = max(0, drop["lands_in"] - arrive)
            threat_at_drop = sum(
                (1.0 if q[3] else 0.65) * clamp((75 - math.dist((x, y), q[2]["pos"][:2])) / 75, 0, 1)
                for q in threats
            )
            if self.safe(drop["pos"], me, obs) > 5 and distance <= 225 and threat_at_drop < (1.25 if hp > 0.62 else 0.75):
                repair = 72 * (1 - hp)
                value = 132 + repair - distance * 0.45 - wait * 2.2 - 46 * threat_at_drop
                if drop.get("holder") == me["id"]:
                    value += 45
                candidates.append((value, drop["id"], "drop", drop["pos"], drop))

        if not retreat:
            for item_id, (kind, item, _) in self.objects.items():
                x, y, z = item["pos"]
                distance = math.dist(xy, (x, y))
                if distance > 110 or self.safe(item["pos"], me, obs) < 6 or z - me["pos"][2] > 39:
                    continue
                risk = sum(max(0, 1 - math.dist((x, y), q[2]["pos"][:2]) / 55) for q in threats)
                if kind == "pickups":
                    improvement = self.pickup_value(me, obs, item["kind"])
                    if improvement is None:
                        continue
                    value = 87 + 4 * improvement[0] if any(w is None for w in me["weapons"]) else 79
                    if item["kind"] == "laser":
                        value += 18
                elif kind == "cubes":
                    value = 91 if me["cubes"] < 10 else 60
                else:
                    if hp < 0.86 and now - self.last_damage < me["stats"]["regen_delay"] + 6:
                        continue
                    if any(q[1] < 64 and q[3] for q in threats):
                        continue
                    value = 47 if me["cubes"] < 10 else 25
                    value += 16 if any(w is None for w in me["weapons"]) else 0
                value -= distance * 0.65 + max(0, abs(z - me["pos"][2]) - 3) * 1.8 + risk * 46
                if self.last_goal == item_id:
                    value += 11
                candidates.append((value, item_id, kind, item["pos"], item))

        if candidates:
            value, item_id, mode, pos, item = max(candidates, key=lambda c: c[:2])
            if value > (47 if retreat else 18):
                self.last_goal = item_id
                return mode, tuple(pos[:2]), urgent, item

        self.last_goal = None
        if next_dist < edge + 24 and not retreat and now < 310:
            spokes = 8
            index = int(now / 18) + me["id"] * 3
            yaw = math.tau * (index % spokes) / spokes
            radius = min(edge * 0.9, 102.0)
            return "patrol", (ahead[0] + radius * math.cos(yaw), ahead[1] + radius * math.sin(yaw)), urgent, None
        return "zone", fallback, urgent, None

    def focus(self, me, obs, threats, retreat, mode, item):
        combatants = []
        hp = me["hp"] / me["max_hp"]
        recovering = retreat and hp < 0.67 and not any(d < 38 and w > 0.8 for w, d, _, _ in threats)
        for weight, distance, enemy, aggressive in threats[:6]:
            if recovering and distance > 39 and enemy.get("hp", 999) > 220:
                continue
            if (mode == "drop" or hp < 0.75) and not aggressive and distance > 75 and enemy.get("hp", 999) > 320:
                continue
            if not aggressive and distance > 120 and enemy.get("hp", 999) > 450:
                continue
            options = target_options(me, enemy, self.config, self.arena)
            if not options:
                continue
            missing = 1 - enemy.get("hp", 1200) / max(1, enemy.get("max_hp", 1400))
            value = max(op[0] for op in options) * 0.55 + weight * 115 + missing * 115 - distance * 0.48
            if self.last_target == enemy["id"]:
                value += 22
            if enemy.get("hp", 999) < 290:
                value += 52
            combatants.append((value, enemy, options))
        if combatants:
            _, target, options = max(combatants, key=lambda c: c[0])
            return target, options
        if mode == "crates" and item and math.dist(me["pos"], item["pos"]) < 82:
            options = target_options(me, item, self.config, self.arena)
            if options:
                return item, options
        if not retreat and mode not in ("drop", "endgame") and hp > 0.8 and not any(q[1] < 78 for q in threats):
            drones = [m for m in obs["mechs"] if m["kind"] == "npc" and math.dist(m["pos"], me["pos"]) < 43]
            for drone in sorted(drones, key=lambda m: m.get("hp", 350))[:2]:
                options = target_options(me, drone, self.config, self.arena)
                if options:
                    return drone, options
        return None, []

    def waypoint(self, me, obs, goal, elevated):
        pos = me["pos"]
        xy = pos[:2]
        radius = me["stats"]["radius"]
        now = obs["t"]
        want_replan = self.route_goal is None or math.dist(goal, self.route_goal) > 12
        if want_replan or now >= self.replan:
            self.route_goal = goal
            if elevated or not me["grounded"] or self.arena.direct(xy, goal, radius):
                self.route = [goal]
                self.replan = now + 0.75
            else:
                self.route = self.arena.route(xy, goal, radius, perf_counter() + 0.005)
                self.replan = now + (0.65 if self.stuck > 4 else 2.2)
        while len(self.route) > 1 and math.dist(xy, self.route[0]) < 3.8:
            self.route.pop(0)
        if len(self.route) > 2 and self.arena.direct(xy, self.route[2], radius):
            del self.route[:2]
        return self.route[0] if self.route else goal

    def projectile_cost(self, me, velocity, projectiles):
        cost = 0.0
        x, y, z = me["pos"]
        radius = me["stats"]["radius"] + 0.6
        height = me["stats"]["height"]
        for p in projectiles:
            px, py, pz = p["pos"]
            vx, vy, vz = p["vel"]
            rx, ry = px - x, py - y
            dx, dy = vx - velocity[0], vy - velocity[1]
            dt = -(rx * dx + ry * dy) / max(1.0, dx * dx + dy * dy)
            if not 0 <= dt <= 0.62:
                continue
            separation = math.hypot(rx + dx * dt, ry + dy * dt)
            shell = p["kind"] == "mortar"
            p_h = pz + vz * dt - (11 * dt * dt if shell else 0)
            us_h = z + me["vel"][2] * dt if not me["grounded"] else z
            if shell:
                if us_h - 2 < p_h < us_h + height + 3:
                    cost += 4.0 * max(0, (radius + 7 - separation) / (radius + 7))
            elif us_h - 0.6 < p_h < us_h + height + 0.6 and separation < radius + 1.5:
                cost += 3.8 * (radius + 1.5 - separation) / (radius + 1.5)
        return cost

    def movement(self, me, obs, threats, goal, mode, item, retreat, urgent):
        xy = me["pos"][:2]
        z = me["pos"][2]
        now = obs["t"]
        height, radius = me["stats"]["height"], me["stats"]["radius"]
        goal_distance = math.dist(xy, goal)
        hover_height = None
        if item and mode in ("cubes", "pickups") and item["pos"][2] > z + 3.5:
            hover_height = item["pos"][2] + 1.8
        if mode == "endgame" and not self.arena.clearance[self.arena.key(*goal)]:
            roofs = [top for base, top, ring, holes in self.arena.columns[self.arena.key(*goal)]
                     if base < z + height and top > z + 2]
            if roofs:
                hover_height = min(roofs) + 1.5
        if hover_height is None and me["grounded"] and self.stuck >= 10 and urgent and me["energy"] > 50:
            hover_height = z + 19
        elevated = hover_height is not None and goal_distance < 43 and hover_height - z < 42
        if elevated and me["grounded"] and me["energy"] > max(20, (hover_height - z) * 1.75):
            self.jet_until = now + clamp((hover_height - z) / 10 + 0.65, 0.55, 4.35)
            self.replan = 0

        waypoint = self.waypoint(me, obs, goal, elevated and self.jet_until > now)
        if mode in ("drop", "endgame") and math.dist(xy, goal) < (4.5 if mode == "drop" else 11):
            waypoint = goal
        walking = me["stats"]["walk_speed"]
        sprinting = me["stats"]["boost_speed"]
        close_threats = [q for q in threats[:4] if q[1] < 150]
        danger = sum(q[0] for q in close_threats if q[3] and q[1] < 105)
        recent_damage = now - self.last_damage < 1.3
        boost = me["grounded"] and me["energy"] > 15 and (
            (urgent and goal_distance > 13) or (retreat and (danger > 0.8 or recent_damage) and me["energy"] > 24)
        )
        if not me["grounded"] or self.jet_until > now:
            boost = self.jet_until > now and me["energy"] > 5 and (
                hover_height is None or z < hover_height + 0.5
            )
        speed = sprinting if boost and me["grounded"] else walking
        angle = bearing(xy, waypoint)
        projectiles = [p for p in obs["projectiles"] if math.dist(p["pos"][:2], xy) < 105][:24]
        max_step = math.dist(xy, waypoint)
        tolerance = 0.9 if mode == "endgame" else 2.4 if mode == "drop" else 1.8
        if mode == "crates":
            tolerance = 24 if item and abs(item["pos"][2] - z) < 12 else 6
        stop = goal_distance < tolerance and (mode in ("drop", "endgame", "crates") or not close_threats)
        candidates = [(0.0, 0.0)] if stop else []
        for i in range(16):
            yaw = angle + (i // 2 + 1) * math.tau / 16 * (-1 if i % 2 == 0 else 1) if i else angle
            dx, dy = direction(yaw)
            # Slow down only when intentionally holding a precise point.
            scale = clamp(max_step / 6.0, 0.24, 1) if mode in ("drop", "endgame") and max_step < 6 else 1
            candidates.append((dx * scale, dy * scale))

        def rate(move):
            dx, dy = move
            sample_dist = 0.68 * speed
            sx, sy = xy[0] + dx * sample_dist, xy[1] + dy * sample_dist
            if not self.arena.in_bounds(sx, sy, radius * 0.55):
                return -1000
            nx, ny = xy[0] + dx * (radius + 1.5), xy[1] + dy * (radius + 1.5)
            z_near = z + max(0, me["vel"][2]) * 0.28
            z_far = z + max(0, me["vel"][2]) * 0.5
            near_block = not self.arena.walkable(nx, ny, z_near, height)
            far_block = not self.arena.walkable(sx, sy, z_far, height)
            arrive = math.dist((sx, sy), waypoint)
            value = (max_step - arrive) * (0.45 if urgent else 0.35)
            if not stop:
                value += (dx * self.last_command[0] + dy * self.last_command[1]) * 0.42
            elif move == (0.0, 0.0):
                value += 1.45
            if near_block or far_block:
                value -= 5.0 * near_block + 3.5 * far_block
            if far_block and near_block and self.jet_until > now:
                value += 4.4
            edge = obs["zone"]["radius"] - math.dist((sx, sy), obs["zone"]["center"])
            if edge < 9:
                old = obs["zone"]["radius"] - math.dist(xy, obs["zone"]["center"])
                value += (edge - old) * (0.4 if urgent else 0.28)
            if close_threats:
                for weight, distance, enemy, hostile in close_threats:
                    ex, ey, ez = enemy["pos"]
                    future = math.dist((sx, sy), (ex + enemy["vel"][0] * 0.45, ey + enemy["vel"][1] * 0.45))
                    delta = future - math.dist(xy, (ex, ey))
                    offset = abs(ez - z)
                    if retreat or distance < 30:
                        value += clamp(delta, -10, 10) * weight * (0.55 if retreat else 0.36) / (1 + offset / 30)
                        if retreat and distance < 85 and self.arena.columns[self.arena.key(sx, sy)]:
                            if not self.arena.visible((sx, sy, z + 5), (ex, ey, ez + 5)):
                                value += 2.65 * weight
                    if hostile and distance < 145:
                        tx, ty = unit(ex - xy[0], ey - xy[1])
                        side = dx * (-ty) + dy * tx
                        orbit = math.sin(now * 1.71 + me["id"] * 0.91)
                        value += abs(side) * 0.55 * weight + side * orbit * 0.40 * weight
            if projectiles:
                velocity = (
                    dx * speed * 0.8 + me["vel"][0] * 0.20,
                    dy * speed * 0.8 + me["vel"][1] * 0.20,
                )
                value -= self.projectile_cost(me, velocity, projectiles)
            return value

        movement = max(candidates, key=rate)
        self.last_command = movement
        jump = me["grounded"] and self.jet_until > now
        if not jump and self.stuck > 11 and now % 1.25 < self.decide_s:
            jump = me["grounded"] and not stop
        return movement, boost, jump, angle

    def act(self, obs):
        me = obs["self"]
        now = obs["t"]
        self.update(me, obs)
        threats = self.assess_threats(me, obs)
        hp = me["hp"] / me["max_hp"]
        pressure = sum(weight for weight, dist, _, aimed in threats[:4] if dist < 105 and aimed)
        full_fight = obs["alive"] <= 3 or obs["zone"]["radius"] < 41
        retreat = (hp < (0.36 if full_fight else 0.64) and pressure > 0.32) or pressure > (3.6 if full_fight else 2.65)
        retreat |= hp < 0.53 and now - self.last_damage < 2.5 and not full_fight
        mode, goal, urgent, item = self.goal(me, obs, threats, retreat)
        target, options = self.focus(me, obs, threats, retreat, mode, item)
        movement, boost, jump, travel_yaw = self.movement(me, obs, threats, goal, mode, item, retreat, urgent)
        aim, fire = fire_control(me, options, target, retreat or urgent, hp)
        if aim is not None:
            self.last_target = target["id"]
        elif self.last_attacker and now - self.last_damage < 2.2:
            aim = [self.last_attacker[1], 0.0]
            self.last_target = None
        elif threats and threats[0][1] < 78:
            aim = [bearing(me["pos"], threats[0][2]["pos"]), 0.0]
            self.last_target = None
        else:
            scan = math.sin(now * 0.94 + me["id"]) * (0.95 if mode == "patrol" else 0.47)
            aim = [travel_yaw + scan, -0.06]
            self.last_target = None

        action = {"tick": obs["tick"], "move": list(movement), "aim": aim, "fire": fire, "boost": boost, "jump": jump}
        gun = next((slot for slot in me["weapons"] if slot and slot["kind"] == "gun"), None)
        if gun and not gun.get("reload", 0):
            ammo = gun.get("ammo", 0)
            action["reload"] = ammo == 0 or (not target and ammo < gun["stats"]["mag"] * 0.78)
        self.pick_actions(action, me, obs)
        return action


if __name__ == "__main__":
    mechfight.run(Bot)
