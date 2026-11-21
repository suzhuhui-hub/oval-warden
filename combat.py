"""Target selection and fire control for documented Mech Royale weapons."""

import math


def angle_delta(a, b):
    return (a - b + math.pi) % math.tau - math.pi


def clamp(value, low, high):
    return max(low, min(high, value))


def bearing(source, target):
    return math.atan2(target[1] - source[1], target[0] - source[0])


def direction(yaw):
    return math.cos(yaw), math.sin(yaw)


def unit(x, y):
    length = math.hypot(x, y)
    return (x / length, y / length) if length > 1e-8 else (0.0, 0.0)


def target_center(target, config):
    kind = target.get("kind", "crate")
    if kind == "mech":
        size = 1 + config["cubes"].get("size_per_cube", 0.05) * target.get("cubes", 0)
        return config["mech"]["radius"] * size, config["mech"]["height"] * size * 0.5
    stats = config["npc"] if kind == "npc" else config["crates"]
    return stats["radius"], stats["height"] * 0.5


def solution(me, target, weapon, config, arena):
    kind = weapon["kind"]
    stats = weapon["stats"]
    radius, half_height = target_center(target, config)
    source = list(me["pos"])
    muzzle = me["stats"].get("muzzle_height", 7)
    source[2] += me["stats"]["height"] + 0.5 if kind == "mortar" else muzzle
    pos = target["pos"]
    target_vel = target.get("vel", (0, 0, 0))
    rotation_delay = min(0.26, abs(angle_delta(bearing(source, pos), me["aim"][0])) / me["stats"]["turn_rate"])
    delay = 0.05 + 0.55 * rotation_delay
    speed = stats.get("speed", config["weapons"][kind].get("speed", 0))
    gravity = stats.get("gravity", config["weapons"][kind].get("gravity", 0)) if kind == "mortar" else 0
    meters = math.dist(source, (pos[0], pos[1], pos[2] + half_height))
    travel = max(0, meters - 3) / speed if speed else 0
    max_range = stats.get("range", 175)
    if meters > max_range + radius:
        return None
    if kind == "mortar" and meters < 9:
        return None

    for _ in range(7 if kind == "mortar" else 3):
        eta = travel + delay
        # Limit vertical extrapolation for a jump already near its apex.
        tz = pos[2] + half_height + clamp(target_vel[2] * eta, -14, 11)
        if arena and target_vel[2] < -1:
            tz = max(tz, arena.terrain(pos[0] + target_vel[0] * eta, pos[1] + target_vel[1] * eta) + half_height)
        aim_pos = (pos[0] + target_vel[0] * eta, pos[1] + target_vel[1] * eta, tz)
        dx, dy = aim_pos[0] - source[0], aim_pos[1] - source[1]
        dz = tz - source[2] + 0.5 * gravity * travel * travel
        horizontal = math.hypot(dx, dy)
        length = math.hypot(horizontal, dz)
        if speed:
            travel = (0.33 * travel + 0.67 * max(0, length - 3) / speed)
    pitch = math.atan2(dz, horizontal)
    if kind == "lightning":
        pitch = clamp(pitch, me["stats"]["min_pitch"], me["stats"]["max_pitch"])
    elif not me["stats"]["min_pitch"] - 0.05 <= pitch <= me["stats"]["max_pitch"] + 0.05:
        return None
    if kind == "mortar" and (travel > 3.4 or abs(length - 3 - speed * travel) > 2.4):
        return None
    if arena:
        if kind == "mortar":
            peak = (
                source[0] + dx * 0.5,
                source[1] + dy * 0.5,
                (source[2] + aim_pos[2]) * 0.5 + 0.125 * gravity * travel * travel,
            )
            if not (arena.visible(source, peak) and arena.visible(peak, aim_pos)):
                return None
        elif not arena.visible(source, aim_pos):
            return None

    yaw_tolerance = math.atan2(radius * 0.81, max(1, horizontal))
    pitch_tolerance = math.atan2(half_height * 0.85, max(1, horizontal))
    if kind == "lightning":
        yaw_tolerance = math.radians(stats.get("cone_deg", 30) * 0.36)
        pitch_tolerance = 2.0
    return math.atan2(dy, dx), pitch, yaw_tolerance, pitch_tolerance, travel, meters


def target_options(me, target, config, arena):
    options = []
    for slot, weapon in enumerate(me["weapons"]):
        if not weapon:
            continue
        kind = weapon["kind"]
        charge, cooldown = weapon.get("charge", 0), weapon.get("cooldown", 0)
        if kind == "lightning" and me["energy"] < weapon["stats"]["energy"]:
            continue
        if cooldown > (0.8 if kind == "gun" else 0.5) and not charge:
            continue
        shot = solution(me, target, weapon, config, arena)
        if shot is None:
            continue
        yaw, pitch, tol_yaw, tol_pitch, travel, meters = shot
        rotation = abs(angle_delta(yaw, me["aim"][0])) / me["stats"]["turn_rate"]
        damage = weapon["stats"]["damage"]
        if kind == "laser" and "mortar" in target.get("weapons", ()):
            damage *= 1 + weapon["stats"].get("cookoff", 0.6)
        if kind == "mortar":
            damage = (damage + weapon["stats"]["splash"] * 0.60) / (1 + 0.33 * travel)
        readiness = max(rotation, cooldown) + (0.14 if kind == "laser" and not charge else 0)
        value = damage / (1 + readiness * 3.0)
        if charge:
            value += 180
        if kind == "gun":
            value *= 2.1 if weapon.get("ammo", 0) > 0 and not weapon.get("reload", 0) else 0.35
        if kind == "lightning":
            value *= 3.2 if "laser" in target.get("weapons", ()) else 2.4
        if travel and meters > 80 and target.get("vel"):
            value *= 1 / (1 + 0.12 * travel)
        options.append((value, slot, weapon, shot))
    return options


def fire_control(me, options, focused, evasive, hp_fraction):
    if not options:
        return None, [False, False]
    _, _, _, primary = max(options, key=lambda item: item[0])
    yaw, pitch = primary[:2]
    fire = [False, False]
    for _, slot, weapon, shot in options:
        expected_yaw, expected_pitch, tyaw, tpitch, _, meters = shot
        cooldown = weapon.get("cooldown", 0)
        actual_yaw, actual_pitch = me["aim"]
        kind = weapon["kind"]
        # A single torso controls both slots. The second shot must share the aim.
        shared = abs(angle_delta(yaw, expected_yaw)) <= tyaw and abs(pitch - expected_pitch) <= tpitch
        aligned = shared and abs(angle_delta(actual_yaw, expected_yaw)) < tyaw + 0.025
        aligned = aligned and abs(actual_pitch - expected_pitch) < tpitch + 0.025
        if kind == "laser":
            charge = weapon.get("charge", 0)
            stats = weapon["stats"]
            desired = stats["charge"]
            full_damage = stats["damage"]
            if "mortar" in focused.get("weapons", ()):
                full_damage *= 1 + stats.get("cookoff", 0.6)
            fraction = (focused.get("hp", full_damage * 2) + 12) / max(1, full_damage)
            if fraction < 0.86:
                desired = max(stats["min_charge"], desired * clamp((fraction - 0.35) / 0.65, 0.18, 1))
            if evasive and (hp_fraction < 0.58 or meters < 48):
                desired = min(desired, max(stats["min_charge"], 0.30))
            if charge > 0:
                timeout = stats["charge"] + min(0.95, stats["hold_s"] * 0.65)
                fire[slot] = charge < timeout and (charge < desired - 0.035 or not aligned)
            elif cooldown <= 0.07 and shared:
                fire[slot] = (not evasive or meters > 45 or hp_fraction > 0.45) and abs(angle_delta(actual_yaw, yaw)) < 0.52
        elif aligned and cooldown < 0.105:
            fire[slot] = kind != "gun" or (weapon.get("ammo", 0) > 0 and weapon.get("reload", 0) < 0.05)
    return [yaw, pitch], fire
