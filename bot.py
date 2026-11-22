"""Oval Warden, a survival-first entry for the Builders Buildathon."""

from collections import deque
import math
from time import perf_counter

from arena import Arena, inside
from combat import angle_delta, bearing, clamp, direction, fire_control, target_options, unit
import mechfight


class Bot:
    name = "Oval Warden Arcade"

    def __init__(self, hello):
        self.config = hello["config"]
        self.arena = Arena(hello["map"]["dir"], self.config["mech"])
        self.route = []
        self.route_goal = None
        self.replan = 0
        self.last_position = None
        self.last_enemy_pos = {}
        self.salvage = None
        self.last_command = (0, 0)
        self.stuck = 0
        self.last_attacker = None
        self.last_damage = -100
        self.last_exchange = -100
        self.incoming = deque()
        self.recovering = False
        self.cover_goal = None
        self.cover_refresh = 0
        self.crossing_ready = 0
        self.sprint_recharging = False
        self.crossing_top = None
        self.circle_anchor = None
        self.anchor_time = 0
        self.last_target = None
        self.last_goal = None
        self.duel_target = None
        self.engaged_id = None
        self.escape_check_until = -1
        self.escape_foe = None
        self.can_escape = False
        self.recent_shooters = {}
        self.sound_tracks = [0.0] * 24
        self.last_sound_time = 0
        self.opening_yaw = None
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
        for entity in obs["mechs"]:
            if entity["kind"] == "mech":
                self.last_enemy_pos[entity["id"]] = (tuple(entity["pos"]), t)
        for event in obs["events"]:
            if event["type"] == "kill" and event["target"] in self.last_enemy_pos:
                where, seen = self.last_enemy_pos[event["target"]]
                if t - seen <= 2.5 and math.dist(xy, where[:2]) < 106:
                    self.salvage = (where, t + 10)
        if self.salvage and (self.salvage[1] < t or (math.dist(xy,self.salvage[0][:2]) < 5
                            and not any(math.dist(xy,c["pos"][:2]) < 20 for c in obs["cubes"]))):
            self.salvage = None
        if self.opening_yaw is None:
            self.opening_yaw = me["aim"][0]
        decay = math.exp(-max(0, t - self.last_sound_time) * 1.9)
        self.last_sound_time = t
        self.sound_tracks = [weight * decay for weight in self.sound_tracks]
        strengths = {"gun": 1.0, "laser": 1.3, "laser_charge": 1.6, "lightning": 1.2,
                     "mortar": 1.0, "jet": 0.85, "boost": 0.8, "step": 0.8, "reload": 0.7}
        for sound in obs["sounds"]:
            contribution = strengths.get(sound["kind"], 0) * {"near": 1.0, "mid": 0.48, "far": 0.07}.get(sound["dist"], 0)
            if contribution:
                slot = round(sound["bearing"] * 24 / math.tau) % 24
                for offset, weight in ((-1, 0.62), (0, 1), (1, 0.62)):
                    i = (slot + offset) % 24
                    self.sound_tracks[i] = min(8, self.sound_tracks[i] + contribution * weight)
        if self.last_position is not None:
            wants_motion = math.hypot(*self.last_command) > 0.75
            progress = math.dist(xy, self.last_position)
            self.stuck = self.stuck + 1 if wants_motion and progress < 0.38 else max(0, self.stuck - 2)
            if self.stuck == 8:
                self.replan = 0
        self.last_position = xy
        while self.incoming and t - self.incoming[0][0] > 2.8:
            self.incoming.popleft()
        for event in obs["events"]:
            if event["type"] == "damaged" and event.get("bearing") is not None:
                self.last_attacker = (event.get("by"), event["bearing"])
                self.last_damage = t
                self.incoming.append((t, event.get("by"), event["amount"]))
            if event["type"] in ("damaged", "hit"):
                self.last_exchange = t
        for key in ("pickups", "cubes", "crates"):
            for item in obs[key]:
                self.objects[item["id"]] = (key, item, t)
        live_ids = {item["id"] for key in ("pickups", "cubes", "crates") for item in obs[key]}
        for item_id, (_, item, seen) in list(self.objects.items()):
            if t - seen > 18 or (item_id not in live_ids and math.dist(me["pos"], item["pos"]) < 11):
                del self.objects[item_id]

    def assess_threats(self, me, obs):
        threats = []
        has_long_laser = any(w and w["kind"] == "laser" for w in me["weapons"])
        attackers = {by for _, by, _ in self.incoming}
        our_pos = me["pos"]
        their_eye = self.config["mech"]["eye_height"]
        our_torso = (our_pos[0], our_pos[1], our_pos[2] + me["stats"]["height"] * 0.60)
        for enemy in obs["mechs"]:
            if enemy["kind"] != "mech":
                continue
            distance = math.dist(me["pos"], enemy["pos"])
            if distance > (280 if has_long_laser else 205):
                continue
            yaw_error = abs(angle_delta(bearing(enemy["pos"], our_pos), enemy["aim"][0]))
            attacker = enemy["id"] in attackers
            aiming_at_us = yaw_error < (0.30 if enemy.get("charging") else 0.57)
            if aiming_at_us and not attacker and distance < 120:
                ex, ey, ez = enemy["pos"]
                aiming_at_us = self.arena.visible((ex, ey, ez + their_eye), our_torso)
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
            return 6.3
        if kind == "mortar":
            return 4.0 if radius > 65 else 2.8
        if kind == "lightning":
            return 5.1 if radius <= 125 else 3.8
        return 4.3 if radius > 70 else 4.1

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
                "plating": 10.2, "nanites": 10.8 if obs["t"] < 315 else 8.0,
                "armor": 9.9, "servos": 9.7, "jets": 8.8, "laser_damage": 8.65,
                "gun_mag": 8.4, "mortar_reload": 7.85, "lightning_efficiency": 8.9,
                "lightning_range": 7.65, "laser_charge": 7.6, "gun_damage": 7.2,
                "gun_rate": 7.1, "reactor": 7.0, "capacitor": 6.4, "gyros": 6.3,
                "mortar_splash": 6.3, "mortar_velocity": 6.25, "gun_velocity": 6.0,
                "sensors": 5.7, "laser_pierce": 4.0, "lightning_chain": 3.8,
            }
            kinds = {w["kind"] for w in me["weapons"] if w}
            if "gun" in kinds:
                gun = next(w for w in me["weapons"] if w and w["kind"] == "gun")
                ratings["gun_mag"] = 11.3 if gun["stats"]["mag"] <= 12 else 9.6
                ratings["gun_damage"] = 10.4 if len(kinds) == 1 else 9.3
                ratings["gun_rate"] = 9.2 if len(kinds) == 1 else 8.5
            if "lightning" in kinds:
                ratings["lightning_range"] = 11.0
            ratings["plating"] = 11.1 if me["hp"] < 0.6 * me["max_hp"] else 10.6
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

    def find_cover(self, me, obs, threats):
        now = obs["t"]
        xy = tuple(me["pos"][:2])
        if now < self.cover_refresh and self.cover_goal is not None:
            return self.cover_goal
        self.cover_refresh = now + 0.9
        self.cover_goal = None
        sources = []
        for weight, distance, enemy, hostile in threats:
            if distance < 140 and (hostile or distance < 48):
                x, y, z = enemy["pos"]
                sources.append(((x, y, z + 7), weight))
                if len(sources) == 3:
                    break
        if not sources and self.last_attacker and now - self.last_damage < 4:
            dx, dy = direction(self.last_attacker[1])
            sources.append(((xy[0] + dx * 56, xy[1] + dy * 56, me["pos"][2] + 6), 1.05))
        if not sources:
            return xy

        away = bearing(sources[0][0], xy)
        best_value, best_point = -math.inf, None
        deadline = perf_counter() + 0.0025
        min_clearance = self.arena.route_clearance(me["stats"]["radius"])
        for distance in (0, 17, 30, 46):
            offsets = (0,) if not distance else (0, -1, 1, -2, 2, -3, 3, 4)
            for offset in offsets:
                if perf_counter() > deadline:
                    self.cover_goal = best_point
                    return best_point
                ux, uy = direction(away + offset * math.tau / 8)
                point = xy if not distance else (xy[0] + distance * ux, xy[1] + distance * uy)
                if not self.arena.in_bounds(*point, min_clearance * self.arena.cell):
                    continue
                cell = self.arena.key(*point)
                if distance and self.arena.clearance[cell] < min_clearance:
                    continue
                if self.safe(point, me, obs) < 7:
                    continue
                altitude = self.arena.terrain(*point) if distance else me["pos"][2]
                eye = (*point, altitude + me["stats"]["height"] * 0.55)
                value = -0.11 * distance
                for origin, weight in sources:
                    meters = math.dist(eye, origin)
                    exposure = self.arena.visible(eye, origin)
                    value += weight * (clamp(meters / 15, 0, 7) - (10.5 if exposure else 0))
                    if meters < 18:
                        value -= 8 * weight
                if distance and not self.arena.direct(xy, point, me["stats"]["radius"]):
                    value -= 2.6
                if value > best_value:
                    best_value, best_point = value, point
        self.cover_goal = best_point
        return best_point

    def goal(self, me, obs, threats, retreat):
        xy = me["pos"][:2]
        zone = obs["zone"]
        now = obs["t"]
        ahead = zone["next_center"]
        next_dist = math.dist(xy, ahead)
        eta = next_dist / me["stats"]["walk_speed"] * 1.28
        outside = math.dist(xy, zone["center"]) - zone["radius"]
        urgent = outside > -12 or (
            next_dist > zone["next_radius"] - 12 and now + eta + 15 > zone["shrink_end"]
        )
        if zone["next_radius"] < 3 or zone["radius"] < 29:
            return "endgame", tuple(ahead), True, None
        # Aim for the near side of the next circle, leaving room to dodge.
        edge = max(0.0, min(zone["next_radius"] - 25, zone["next_radius"] * 0.63))
        vector = unit(xy[0] - ahead[0], xy[1] - ahead[1])
        fallback = (ahead[0] + edge * vector[0], ahead[1] + edge * vector[1])
        if outside > 1:
            return "zone", self.circle_route(me, obs, threats, fallback, True), urgent, None

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
            active = drop.get("holder") == me["id"]
            arrival = (distance * 1.20) / (me["stats"]["walk_speed"] * 1.18)
            strategic = me["cubes"] < 10 or hp < 0.88
            max_distance = min(365, 165 + drop["lands_in"] * 8.2) if strategic else 130
            if self.safe(drop["pos"], me, obs) > 8 and distance <= max_distance and threat_at_drop < (1.5 if hp > 0.66 else 0.85):
                repair = 111 * (1 - hp)
                upgrade = 45 if me["cubes"] < 7 else 27 if me["cubes"] < 10 else 5
                value = 128 + repair + upgrade - arrival * 2.4 - wait * 0.64 - 43 * threat_at_drop
                if active:
                    value += 45
                elif drop.get("holder") is not None and drop.get("progress", 0) > 0.5:
                    value -= max(0, arrival - 0.8) * 9
                if self.last_goal == drop["id"]:
                    value += 18
                candidates.append((value, drop["id"], "drop", drop["pos"], drop))

        if urgent:
            for value, item_id, mode, pos, item in sorted(candidates, key=lambda c: -c[0]):
                if value > 57 and math.dist(pos[:2], ahead) < zone["next_radius"] - 18:
                    self.last_goal = item_id
                    return mode, tuple(pos[:2]), urgent, item
            return "zone", self.circle_route(me, obs, threats, fallback, True), urgent, None

        best_capture = None
        for _, _, _, pos, drop in candidates:
            if drop.get("holder") not in (None, me["id"]):
                continue
            remaining = drop["capture_s"] - drop.get("progress", 0)
            gap = max(0, math.dist(xy, pos[:2]) - drop["radius"] + 1)
            run = gap / me["stats"]["boost_speed"]
            complete_in = max(run, drop["lands_in"]) + remaining
            if complete_in > (7.0 if hp < 0.80 else 5.4):
                continue
            protected = drop.get("holder") == me["id"] and remaining <= 1.2
            contest = min((math.dist(e["pos"][:2], pos[:2]) for _, _, e, _ in threats), default=999)
            if not protected and contest < drop["radius"] + 7.5 * complete_in:
                continue
            if gap > 3 and not self.arena.direct(xy, pos[:2], me["stats"]["radius"]):
                continue
            if best_capture is None or complete_in < best_capture[0]:
                best_capture = complete_in, drop
        if best_capture:
            chosen = best_capture[1]
            self.last_goal = chosen["id"]
            return "drop", tuple(chosen["pos"][:2]), False, chosen

        if any(w is None for w in me["weapons"]):
            finish = self.duel_target and self.duel_target.get("hp", 1400) < 220
            nearby = []
            for kind, pickup, last_seen in self.objects.values():
                if kind != "pickups" or pickup["kind"] == "gun" or now-last_seen > 7:
                    continue
                pos = pickup["pos"]
                gap = math.dist(xy,pos[:2])
                if gap > (24 if finish else 79) or abs(me["pos"][2]-pos[2]) > 4.5 or self.safe(pos,me,obs) < 10:
                    continue
                direct = self.arena.direct(xy,pos[:2],me["stats"]["radius"])
                if not direct and gap > 48:
                    continue
                cost = gap / me["stats"]["boost_speed"] * (1.0 if direct else 1.7)
                for _, distance, foe, active in threats[:3]:
                    their_gap = math.dist(foe["pos"][:2],pos[:2])
                    if active and "lightning" in foe.get("weapons", ()):
                        cost += max(0,49-their_gap)*0.13
                    if their_gap < gap and their_gap < 31:
                        cost += 4
                    if pickup["kind"] == "lightning" and "laser" in foe.get("weapons", ()) and their_gap < 60:
                        cost -= 1.3
                if pickup["kind"] == "laser":
                    cost -= 0.85
                if self.last_goal == pickup["id"]:
                    cost -= 0.5
                nearby.append((cost,pickup["id"],pickup))
            if nearby:
                score, pid, pickup = min(nearby, key=lambda entry: entry[:2])
                if score <= (5.2 if self.duel_target is not None else 6.1):
                    self.last_goal = pid
                    return "pickups", tuple(pickup["pos"][:2]), False, pickup
        if self.salvage and me["cubes"] < 10:
            point = self.salvage[0]
            exposed = sum(1 for _, d, e, active in threats if active and d < 61)
            if exposed < 2 and self.safe(point, me, obs) > 11 and abs(point[2] - me["pos"][2]) < 9:
                caches = [c for c in obs["cubes"] if math.dist(c["pos"],point) < 22]
                item = min(caches, key=lambda c: math.dist(c["pos"],me["pos"])) if caches else {"pos":point}
                return "salvage", tuple(item["pos"][:2]), False, item
        if self.duel_target is not None:
            enemy = self.duel_target
            accessible = [obj for obj in obs["cubes"] if
                          abs(obj["pos"][2] - me["pos"][2]) < 3.3
                          and math.dist(xy, obj["pos"][:2]) < (24 if me["cubes"] < 3 else 14)
                          and self.safe(obj["pos"], me, obs) > 8
                          and self.arena.direct(xy, obj["pos"][:2], me["stats"]["radius"])]
            if accessible:
                cube = min(accessible, key=lambda obj: math.dist(xy, obj["pos"][:2]))
                return "cubes", tuple(cube["pos"][:2]), False, cube
            # Grab a second weapon if it is only a few steps from our line of travel.
            if any(w is None for w in me["weapons"]):
                nearby = [p for p in obs["pickups"] if math.dist(me["pos"], p["pos"]) <= 18]
                if nearby:
                    pick = min(nearby, key=lambda p: math.dist(me["pos"], p["pos"]))
                    return "pickups", tuple(pick["pos"][:2]), False, pick
            gap = math.dist(xy, enemy["pos"][:2])
            own = {w["kind"] for w in me["weapons"] if w}
            their = enemy.get("weapons", ())
            if "lightning" in own and ("laser" in their or "laser" not in own):
                arc = next(w["stats"]["range"] for w in me["weapons"] if w and w["kind"] == "lightning")
                spacing = min(arc * 0.70, 41)
            elif "laser" in own:
                spacing = 131 if "laser" not in their else 90
            else:
                spacing = 81 if "lightning" in their else 63
            if enemy.get("hp", 1400) < min(me["hp"] * 0.46, 460) and me["cubes"] < 10:
                spacing = min(spacing, 17 if "lightning" not in enemy.get("weapons", ()) else 32)
            away = bearing(enemy["pos"], xy)
            side = 1 if math.sin(me["id"] + int(now / 4.1)) >= 0 else -1
            tangent = side * clamp(12 / max(gap, 1), 0.10, 0.59)
            desired = (enemy["pos"][0] + spacing * math.cos(away + tangent),
                       enemy["pos"][1] + spacing * math.sin(away + tangent))
            if self.safe(desired, me, obs) > 8:
                return "duel", desired, False, enemy

        if retreat:
            recover_position = self.find_cover(me, obs, threats)
            if recover_position is not None:
                candidates.append((79 if self.recovering else 60, -1, "cover", recover_position, None))
        else:
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
                    value = 126 if me["cubes"] < 3 else 104 if me["cubes"] < 10 else 59
                else:
                    if hp < 0.86 and now - self.last_damage < me["stats"]["regen_delay"] + 6:
                        continue
                    if any(q[1] < 64 and q[3] for q in threats):
                        continue
                    value = 69 if me["cubes"] < 6 else 48 if me["cubes"] < 10 else 22
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
        if next_dist < edge + 24 and not retreat and now < 100:
            spokes = 8
            index = int(now / 18) + me["id"] * 3
            yaw = math.tau * (index % spokes) / spokes
            radius = min(edge * 0.9, 102.0)
            return "patrol", (ahead[0] + radius * math.cos(yaw), ahead[1] + radius * math.sin(yaw)), urgent, None
        return "zone", self.circle_route(me, obs, threats, fallback, False), urgent, None

    def circle_route(self, me, obs, threats, fallback, urgent):
        zone = obs["zone"]
        center = zone["next_center"]
        radius = zone["next_radius"]
        now = obs["t"]
        xy = me["pos"][:2]
        if self.circle_anchor and now < self.anchor_time and math.dist(self.circle_anchor, center) < radius - 9:
            return self.circle_anchor
        self.anchor_time = now + (0.95 if urgent else 2.6)
        line = bearing(center, xy)
        radius0 = max(0, min(radius - 25, radius * 0.63))
        focus_radius = max(0, min(radius - 23, radius * 0.35))
        min_clearance = self.arena.route_clearance(me["stats"]["radius"])
        nearest_threats = [(weight, enemy) for weight, _, enemy, hostile in threats if hostile][:3]
        best = None
        for radius_hint in (radius0, focus_radius):
            for step in (0, -1, 1, -2, 2, -3, 3, 4):
                dx, dy = direction(line + step * math.pi / 4)
                wanted = (center[0] + dx * radius_hint, center[1] + dy * radius_hint)
                if not self.arena.in_bounds(*wanted, 5):
                    continue
                index = self.arena.nearest(*wanted, minimum=min_clearance, max_ring=5)
                candidate = self.arena.center(index)
                if self.arena.clearance[index] < min_clearance or self.safe(candidate, me, obs) < 11:
                    continue
                distance = math.dist(xy, candidate)
                score = -distance * (0.51 if urgent else 0.29)
                if not self.arena.direct(xy, candidate, me["stats"]["radius"]):
                    score -= 14 if urgent else 8
                if not urgent:
                    score += min(18, radius - math.dist(candidate, center)) * 0.68
                if self.arena.clearance[index] in (1, 2, 3):
                    score += 6
                height = self.arena.terrain(*candidate) + 5
                for weight, enemy in nearest_threats:
                    ex, ey, ez = enemy["pos"]
                    gap = math.dist(candidate, (ex, ey))
                    score += weight * min(16, gap * 0.14)
                    if gap < 150 and not self.arena.visible((*candidate, height), (ex, ey, ez + 7)):
                        score += 9 * weight
                    elif gap < 45:
                        score -= 10 * weight
                if best is None or score > best[0]:
                    best = (score, candidate)
        self.circle_anchor = best[1] if best else tuple(fallback)
        return self.circle_anchor

    def focus(self, me, obs, threats, retreat, mode, item):
        if self.duel_target is not None:
            options = target_options(me, self.duel_target, self.config, self.arena)
            if options:
                return self.duel_target, options
        combatants = []
        hp = me["hp"] / me["max_hp"]
        for weight, distance, enemy, aggressive in threats[:6]:
            enemy_hp = enemy.get("hp", 999)
            # A distant hit resets our own repair counter, even while behind cover.
            if retreat and (distance > 50 or enemy_hp > 335) and (distance > 33 or not aggressive):
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
            if enemy_hp < 415:
                value += 68 + (415 - enemy_hp) * 0.22
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

    def waypoint(self, me, obs, goal, elevated, zone_route=False):
        pos = me["pos"]
        xy = pos[:2]
        radius = me["stats"]["radius"]
        now = obs["t"]
        circle = obs["zone"]
        circle_goal = tuple(circle["next_center"])
        circle_radius = max(0.0, circle["next_radius"] - 12)
        if zone_route and circle_radius > 5 and math.dist(xy, circle_goal) > circle_radius:
            goal = circle_goal
            want_replan = self.route_goal != goal or now >= self.replan or not self.route
            if want_replan:
                self.route_goal = goal
                self.route = self.arena.route(xy, goal, radius, perf_counter() + 0.008,
                                              goal_radius=circle_radius)
                self.replan = now + (0.7 if self.stuck > 4 else 2.3)
        else:
            self.ground_route(me, goal, elevated, now)
        while len(self.route) > 1 and math.dist(xy, self.route[0]) < 4.2:
            self.route.pop(0)
        if len(self.route) > 2 and self.arena.direct(xy, self.route[2], radius):
            del self.route[:2]
        return self.route[0] if self.route else goal

    def ground_route(self, me, goal, elevated, now):
        xy = me["pos"][:2]
        radius = me["stats"]["radius"]
        want_replan = self.route_goal is None or math.dist(goal, self.route_goal) > 12
        if want_replan or now >= self.replan:
            self.route_goal = goal
            if elevated or not me["grounded"] or self.arena.direct(xy, goal, radius):
                self.route = [goal]
                self.replan = now + 0.75
            else:
                self.route = self.arena.route(xy, goal, radius, perf_counter() + 0.008)
                self.replan = now + (0.65 if self.stuck > 4 else 2.2)

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

    def crossing(self, me, goal):
        x, y, z = me["pos"]
        tx, ty = unit(goal[0] - x, goal[1] - y)
        if math.dist((x, y), goal) < 35:
            return None
        obstacles = []
        distance_to_first = math.inf
        for distance in (5, 9, 13, 17, 22, 28, 35):
            px, py = x + tx * distance, y + ty * distance
            for base, top, ring, holes in self.arena.columns[self.arena.key(px, py)]:
                if base < z + 8 and top > z + 3 and inside(px, py, ring) and not any(inside(px, py, h) for h in holes):
                    obstacles.append(top)
                    distance_to_first = min(distance_to_first, distance)
        if not obstacles or distance_to_first > 23:
            return None
        height = max(obstacles) + 2.2
        rise = height - z
        if rise > 32 or me["energy"] < 2.0 * rise + 9:
            return None
        return height

    def movement(self, me, obs, threats, goal, mode, item, target, retreat, urgent):
        xy = me["pos"][:2]
        z = me["pos"][2]
        now = obs["t"]
        height, radius = me["stats"]["height"], me["stats"]["radius"]
        goal_distance = math.dist(xy, goal)
        hover_height = None
        if self.jet_until <= now:
            self.crossing_top = None
        if item and mode in ("cubes", "pickups") and item["pos"][2] > z + 3.5:
            hover_height = item["pos"][2] + 1.8
        if mode == "endgame" and not self.arena.clearance[self.arena.key(*goal)]:
            roofs = [top for base, top, ring, holes in self.arena.columns[self.arena.key(*goal)]
                     if base < z + height and top > z + 2 and inside(*goal, ring)
                     and not any(inside(*goal, hole) for hole in holes)]
            if roofs:
                hover_height = max(roofs) + 1.5
        if hover_height is None and me["grounded"] and self.stuck >= 10 and urgent and me["energy"] > 50:
            hover_height = z + 19
        elevated = hover_height is not None and goal_distance < 43 and hover_height - z < 42
        if elevated and me["grounded"] and self.jet_until <= now and me["energy"] > max(20, (hover_height - z) * 1.75):
            self.jet_until = now + clamp((hover_height - z) / 10 + 0.65, 0.55, 4.35)
            self.replan = 0

        waypoint = self.waypoint(me, obs, goal, elevated and self.jet_until > now,
                                 zone_route=mode == "zone" and urgent and me["grounded"]
                                 and self.stuck > 4 and self.jet_until <= now)
        if mode == "zone" and urgent and me["grounded"] and now >= self.crossing_ready and me["energy"] > 56:
            self.crossing_ready = now + 1.4
            if goal_distance > 51 and self.jet_until <= now and (
                self.stuck >= 6 or abs(angle_delta(bearing(xy, waypoint), bearing(xy, goal))) > 0.78
            ):
                top = self.crossing(me, goal)
                if top is not None:
                    self.crossing_top = top
                    self.crossing_ready = now + 7.1
                    self.jet_until = now + min(4.35, (top - z) / 9.8 + 0.87)
        if self.crossing_top is not None:
            hover_height = self.crossing_top
            waypoint = goal
        if mode in ("drop", "endgame") and math.dist(xy, goal) < (4.5 if mode == "drop" else 11):
            waypoint = goal
        walking = me["stats"]["walk_speed"]
        sprinting = me["stats"]["boost_speed"]
        close_threats = [q for q in threats[:4] if q[1] < 150]
        danger = sum(q[0] for q in close_threats if q[3] and q[1] < 105)
        recent_damage = now - self.last_damage < 1.3
        closing_laser = any(q[2].get("charging") and q[3] and q[1] < 155 for q in close_threats)
        boost_burst = (now + me["id"] * 0.18) % 3.6 < 1.25
        if me["energy"] < 39:
            self.sprint_recharging = True
        elif me["energy"] > 84:
            self.sprint_recharging = False
        lone_duel = (mode == "duel" and len(close_threats) == 1 and close_threats[0][3]
                     and all(w is None or w["kind"] == "gun" for w in me["weapons"]))
        mobile_duel = lone_duel and not self.sprint_recharging
        drop_sprint = mode == "drop" and goal_distance > 23 and (
            me["energy"] > 64 or (me["energy"] > 33 and item and item["lands_in"] < 1.0)
        )
        boost = me["grounded"] and me["energy"] > 15 and (
            (mode == "duel" and self.duel_target is not None and
             any(w and w["kind"] == "lightning" for w in me["weapons"]) and
             "laser" in self.duel_target.get("weapons", ()) and
             math.dist(xy,self.duel_target["pos"][:2]) > 41 and me["energy"] > 43) or
            (urgent and goal_distance > 13) or
            (mode == "salvage" and goal_distance > 8 and me["energy"] > 30) or
            (mode == "pickups" and not all(me["weapons"]) and goal_distance > 8 and me["energy"] > 43) or
            (closing_laser and me["energy"] > 22) or
            (retreat and (danger > 0.8 or recent_damage) and me["energy"] > 24) or
            drop_sprint or mobile_duel or (boost_burst and me["energy"] > 47 and danger > 1.05)
        )
        if not me["grounded"] or self.jet_until > now:
            boost = self.jet_until > now and me["energy"] > 5 and (
                hover_height is None or z < hover_height + 0.5
            )
        speed = sprinting if boost and me["grounded"] else walking
        if any(weapon and weapon.get("charge", 0) > 0 for weapon in me["weapons"]):
            speed *= me["stats"].get("charge_move_mult", 0.5)
        angle = bearing(xy, waypoint)
        projectiles = sorted(obs["projectiles"], key=lambda p: math.dist(p["pos"][:2], xy))[:16]
        projectiles = [p for p in projectiles if math.dist(p["pos"][:2], xy) < 120]
        max_step = math.dist(xy, waypoint)
        tolerance = {"endgame": 0.9, "drop": 2.4, "cover": 3.3}.get(mode, 1.8)
        if mode == "crates":
            tolerance = 27 if target and item and target["id"] == item["id"] else 2.5
        stop = goal_distance < tolerance and (mode in ("drop", "endgame", "crates", "cover") or not close_threats)
        candidates = [(0.0, 0.0)] if stop else []
        for i in range(-7, 9):
            yaw = angle + i * math.tau / 16
            dx, dy = direction(yaw)
            # Slow down only when intentionally holding a precise point.
            scale = clamp(max_step / 6.0, 0.24, 1) if mode in ("drop", "endgame", "cover") and max_step < 6 else 1
            candidates.append((dx * scale, dy * scale))

        # Estimate how much of the next movement is spent turning the legs.
        accel = me["stats"]["ground_accel"] if me["grounded"] else me["stats"]["air_accel"]
        inertia = clamp(speed / (0.68 * accel), 0.14, 0.74)
        terrain_base = self.arena.terrain(*xy) if me["grounded"] else z
        chase_id = None
        preferred_range = 49
        if target and target.get("kind") == "mech" and not retreat and not urgent and mode != "drop":
            if target.get("hp", 999) < 0.41 * me["hp"] and danger < 2.7:
                chase_id = target["id"]
                if any(w and w["kind"] == "lightning" for w in me["weapons"]):
                    preferred_range = 30
        cover_threats = [(w, e) for w, d, e, hostile in close_threats if hostile and d < 108][:2]
        target_id = target["id"] if target else None
        can_trade = any(w and w.get("cooldown", 0) < 0.18 and not w.get("reload", 0) for w in me["weapons"])
        cover_bias = 1.0 if retreat else 0.47 if mode == "drop" and goal_distance < 5 else 0.83

        def rate(move):
            dx, dy = move
            vx = dx * speed * (1 - inertia) + me["vel"][0] * inertia
            vy = dy * speed * (1 - inertia) + me["vel"][1] * inertia
            sx, sy = xy[0] + vx * 0.68, xy[1] + vy * 0.68
            if not self.arena.in_bounds(sx, sy, radius * 0.55):
                return -1000
            nx, ny = xy[0] + dx * (radius + 1.5), xy[1] + dy * (radius + 1.5)
            z_near = z + max(0, me["vel"][2]) * 0.28
            z_far = z + max(0, me["vel"][2]) * 0.5
            if me["grounded"]:
                z_near += clamp(self.arena.terrain(nx, ny) - terrain_base, 0, 1.6)
                z_far += clamp(self.arena.terrain(sx, sy) - terrain_base, 0, 3.4)
            near_block = not self.arena.walkable(nx, ny, z_near, height)
            far_block = not self.arena.walkable(sx, sy, z_far, height)
            shoulder_x, shoulder_y = -dy * radius, dx * radius
            if (dx or dy) and not near_block:
                near_block = any(
                    not self.arena.walkable(nx + sign * shoulder_x, ny + sign * shoulder_y, z_near, height)
                    for sign in (-1, 1)
                )
            immediate_block = False
            if me["grounded"] and (dx or dy):
                tx, ty = xy[0] + dx * 1.25, xy[1] + dy * 1.25
                tz = z + clamp(self.arena.terrain(tx, ty) - terrain_base, 0, 0.65)
                immediate_block = any(
                    not self.arena.walkable(tx + ox * radius, ty + oy * radius, tz, height)
                    for ox, oy in ((0, 0), (0.86, 0), (-0.86, 0), (0, 0.86), (0, -0.86))
                )
            arrive = math.dist((sx, sy), waypoint)
            value = (max_step - arrive) * (0.45 if urgent else 0.35)
            if not stop:
                value += (dx * self.last_command[0] + dy * self.last_command[1]) * 0.42
            elif move == (0.0, 0.0):
                value += 3.5 if mode in ("drop", "cover") else 1.45
            if near_block or far_block:
                value -= 5.0 * near_block + 3.5 * far_block
            if immediate_block:
                value -= 10.5
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
                    if retreat or distance < 27:
                        repel = 0.31 if mode == "cover" else 0.58 if retreat else 0.29
                        value += clamp(delta, -10, 10) * weight * repel / (1 + offset / 30)
                    if chase_id == enemy["id"]:
                        progress = abs(distance - preferred_range) - abs(future - preferred_range)
                        value += clamp(progress, -10, 10) * 0.43
                    if hostile and distance < 145:
                        tx, ty = unit(ex - xy[0], ey - xy[1])
                        side = (vx * (-ty) + vy * tx) / max(1.0, speed)
                        orbit = math.sin(now * 2.2 + me["id"] * 0.91 + math.sin(now * 0.7))
                        value += abs(side) * 1.10 * weight + side * orbit * 0.51 * weight
                        if lone_duel and distance > 23 and weight > 0.48:
                            side_speed = abs(vy * tx - vx * ty)
                            flight = distance / self.config["weapons"]["gun"]["speed"]
                            miss = side_speed * flight / max(radius, 1)
                            value += weight * 3.1 * min(1.35, miss)
                        if enemy.get("charging"):
                            before = abs(angle_delta(enemy["aim"][0], bearing((ex, ey), xy)))
                            after = abs(angle_delta(enemy["aim"][0], bearing((ex, ey), (sx, sy))))
                            value += clamp((after - before) * distance, -8, 8) * 0.44 * weight
            if not urgent and cover_threats:
                torso = (sx, sy, z_far + height * 0.57)
                for weight, enemy in cover_threats:
                    ex, ey, ez = enemy["pos"]
                    source = (ex + enemy["vel"][0] * 0.22, ey + enemy["vel"][1] * 0.22, ez + 7)
                    if not self.arena.visible(torso, source):
                        primary = can_trade and enemy["id"] == target_id and not retreat
                        value += weight * (0.46 if primary else 3.8) * cover_bias
            if projectiles:
                value -= self.projectile_cost(me, (vx, vy), projectiles) * 1.65
            return value

        movement = max(candidates, key=rate)
        self.last_command = movement
        jump = me["grounded"] and self.jet_until > now
        if not jump and self.stuck > 11 and now % 1.25 < self.decide_s:
            jump = me["grounded"] and not stop
        return movement, boost, jump, angle

    def reachable_cover(self, me, obs, foe):
        now = obs["t"]
        if self.escape_foe == foe["id"] and now < self.escape_check_until:
            return self.can_escape
        self.escape_check_until, self.escape_foe = now + 1.0, foe["id"]
        self.can_escape = False
        origin = (*foe["pos"][:2], foe["pos"][2]+7)
        xy = me["pos"][:2]
        away = bearing(origin, xy)
        for reach in (8, 15, 24):
            for side in (0, -1, 1, -2, 2, -3, 3, 4):
                angle = away + side*math.pi/4
                p = (xy[0]+reach*math.cos(angle), xy[1]+reach*math.sin(angle))
                if self.safe(p, me, obs) < 8 or not self.arena.direct(xy, p, me["stats"]["radius"]):
                    continue
                z = self.arena.terrain(*p)
                if not self.arena.visible(origin, (*p, z+me["stats"]["height"]*0.62)):
                    self.can_escape = True
                    return True
        return False

    def act(self, obs):
        me = obs["self"]
        now = obs["t"]
        self.update(me, obs)
        threats = self.assess_threats(me, obs)
        hp = me["hp"] / me["max_hp"]
        pressure = sum(weight for weight, dist, _, aimed in threats[:4] if dist < 105 and aimed)
        full_fight = obs["alive"] <= 3 or obs["zone"]["radius"] < 41
        recent_hurt = sum(amount for _, _, amount in self.incoming)
        repair_threshold = 0.79 if not full_fight else 0.44
        if hp > 0.92 or (full_fight and hp >= 0.64):
            self.recovering = False
        elif (hp < repair_threshold and (pressure > 0.49 or now - self.last_damage < 3)) or (
            hp < 0.84 and not full_fight and recent_hurt > 0.20 * me["max_hp"]
        ):
            finishable = any(d < 45 and e.get("hp", 999) < 220 for _, d, e, _ in threats)
            self.recovering = self.recovering or not (finishable and hp >= 0.48 and pressure < 2.5)
        retreat = self.recovering or pressure > (3.7 if full_fight else 2.36)
        self.duel_target = None
        kinds = {w["kind"] for w in me["weapons"] if w}
        reach = 244 if "laser" in kinds else 160 if "lightning" in kinds else 123
        close = [entry for entry in threats if entry[1] < max(reach, 137)]
        for e in obs["events"]:
            if e["type"] == "damaged" and e.get("by") is not None:
                self.recent_shooters[e["by"]] = now
        choices = []
        shooters = 0
        our_muzzle = (*me["pos"][:2], me["pos"][2] + me["stats"]["muzzle_height"])
        for _, distance, foe, _ in close[:7]:
            if distance > reach:
                continue
            x, y, z = foe["pos"]
            foe_size = 1 + self.config["cubes"]["size_per_cube"] * foe.get("cubes", 0)
            if not self.arena.visible(our_muzzle, (x, y, z + 4.5 * foe_size)):
                continue
            face = abs(angle_delta(foe["aim"][0], bearing(foe["pos"], me["pos"])))
            firing = now - self.recent_shooters.get(foe["id"], -100) < 3.8
            closing = sum((me["pos"][k] - foe["pos"][k]) * foe["vel"][k] for k in (0,1)) / max(1, distance)
            hostile = firing or (face < 0.30 and (closing > 1.3 or distance < 85 or foe.get("charging", False)))
            shooters += hostile
            damage_rate = 102 * (1.0 if distance < 80 else 0.78 if distance < 134 else 0.59)
            if "laser" in kinds:
                damage_rate += 127 * (1.6 if "mortar" in foe.get("weapons", ()) else 1)
            if "lightning" in kinds and distance < 44:
                damage_rate += 195
            if "mortar" in kinds and distance < 130:
                damage_rate += 74
            damage_rate *= (1.50 if face > math.pi/2 + 0.10 else 1.0) * (1+0.04*min(me["cubes"], 10))
            eta = foe.get("hp", 1400) / max(1, damage_rate)
            turn = abs(angle_delta(me["aim"][0], bearing(me["pos"], foe["pos"]))) / me["stats"]["turn_rate"]
            cost = eta + 1.5 * turn + max(0, distance-95)*0.022
            if hostile:
                cost *= 0.72
            if self.engaged_id == foe["id"]:
                cost -= 1.5
            choices.append((cost, eta, distance, foe))
        if choices and obs["zone"]["radius"] >= 40:
            _, eta, distance, foe = min(choices, key=lambda q:q[0])
            if len(close) == 1:
                committed = hp > 0.67 or me["hp"] * (1.42 if len(kinds)>1 else 1.15) >= foe.get("hp",1400)
            else:
                committed = (shooters <= 1 and (hp > 0.38 or eta < 5.8)) or (
                    shooters == 2 and (hp > 0.62 or eta < 4.4)) or eta < 2.4
            if not committed and distance < 112 and (shooters < 2 or eta < 6.0):
                committed = not self.reachable_cover(me, obs, foe)
            if committed:
                self.duel_target = foe
                self.engaged_id = foe["id"]
                retreat = self.recovering = False
        mode, goal, urgent, item = self.goal(me, obs, threats, retreat)
        target, options = self.focus(me, obs, threats, retreat, mode, item)
        movement, boost, jump, travel_yaw = self.movement(me, obs, threats, goal, mode, item, target, retreat, urgent)
        aim, fire = fire_control(me, options, target, retreat or urgent, hp)
        if aim is not None:
            self.last_target = target["id"]
        elif self.last_attacker and now - self.last_damage < 2.2:
            aim = [self.last_attacker[1], 0.0]
            self.last_target = None
        elif threats and threats[0][1] < 78:
            aim = [bearing(me["pos"], threats[0][2]["pos"]), 0.0]
            self.last_target = None
        elif max(self.sound_tracks) >= 0.39 and not target:
            sector = max(range(24), key=self.sound_tracks.__getitem__)
            aim = [sector * math.tau / 24, -0.03]
            self.last_target = None
        elif now < 2.2 and not target and not all(me["weapons"]):
            aim = [self.opening_yaw + min(now * 3.35, math.tau), -0.055]
            self.last_target = None
        else:
            scan = math.sin(now * 0.94 + me["id"]) * (0.95 if mode == "patrol" else 0.47)
            aim = [travel_yaw + scan, -0.06]
            self.last_target = None

        action = {"tick": obs["tick"], "move": list(movement), "aim": aim, "fire": fire, "boost": boost, "jump": jump}
        gun = next((slot for slot in me["weapons"] if slot and slot["kind"] == "gun"), None)
        if gun and not gun.get("reload", 0):
            ammo = gun.get("ammo", 0)
            quiet = not target or mode == "cover" and now - self.last_exchange > 0.65
            quiet_for_reload = not close or now - self.last_exchange >= 2.1
            action["reload"] = ammo == 0 or (quiet and quiet_for_reload and ammo < gun["stats"]["mag"] * 0.82)
        self.pick_actions(action, me, obs)
        return action


if __name__ == "__main__":
    mechfight.run(Bot)
