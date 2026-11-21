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


def future_target(target, eta, arena):
    x, y, z = target["pos"]
    vx, vy, vz = target.get("vel", (0, 0, 0))
    ax, ay, az = target.get("accel", (0, 0, 0))
    # Steering observations are most predictive over the next few decisions.
    horizon = min(0.34, eta)
    drift = horizon * (0.5 * horizon + 0.44 * (eta - horizon))
    x += vx * eta + ax * drift
    y += vy * eta + ay * drift
    z += clamp(vz * eta + az * drift, -19, 14)
    if arena and vz < -1:
        z = max(z, arena.terrain(x, y))
    return x, y, z


def solution(me, target, weapon, config, arena):
    kind = weapon["kind"]
    stats = weapon["stats"]
    radius, half_height = target_center(target, config)
    source = list(me["pos"])
    muzzle = me["stats"].get("muzzle_height", 7)
    source[2] += me["stats"]["height"] + 0.5 if kind == "mortar" else muzzle
    pos = target["pos"]
    rotation_delay = min(0.26, abs(angle_delta(bearing(source, pos), me["aim"][0])) / me["stats"]["turn_rate"])
    delay = clamp(weapon.get("cooldown", 0), 0, 0.075) + 0.035 + 0.40 * rotation_delay
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
        tx, ty, tz = future_target(target, eta, arena)
        tz += half_height
        aim_pos = (tx, ty, tz)
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
            steps = max(3, math.ceil(horizontal / 3.8))
            for i in range(1, steps):
                f = i / steps
                arc_z = source[2] + (aim_pos[2] - source[2]) * f + 0.5 * gravity * travel ** 2 * f * (1 - f)
                if arena.solid(source[0] + dx * f, source[1] + dy * f, arc_z):
                    return None
        elif not arena.visible(source, aim_pos):
            if kind == "lightning":
                return None
            # A roof line may hide the center while leaving part of the mech visible.
            for lift in (0.57 * half_height, -0.50 * half_height):
                adjusted = (aim_pos[0], aim_pos[1], aim_pos[2] + lift)
                adjusted_pitch = math.atan2(adjusted[2] - source[2], horizontal)
                if me["stats"]["min_pitch"] <= adjusted_pitch <= me["stats"]["max_pitch"] and arena.visible(source, adjusted):
                    pitch = adjusted_pitch
                    half_height *= 0.45
                    break
            else:
                return None

    yaw_tolerance = math.atan2(radius * 0.81, max(1, horizontal))
    pitch_tolerance = math.atan2(half_height * 0.81, max(1, horizontal))
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


def charge_needed(me, target, spec, evasive, hp_fraction, meters):
    damage = spec["damage"]
    if "mortar" in target.get("weapons", ()):
        damage *= 1 + spec.get("cookoff", 0.6)
    if "pos" in target and "aim" in target:
        back = abs(angle_delta(target["aim"][0], bearing(target["pos"], me["pos"])))
        if back > math.radians(102):
            damage *= me["stats"].get("rear_mult", 1.5) * 0.95
    floor = spec.get("min_frac", 0.35)
    fraction = (target.get("hp", damage * 2) + 24) / max(1, damage)
    desired = clamp((fraction - floor) / (1 - floor), 0, 1) * spec["charge"]
    if evasive and (hp_fraction < 0.58 or meters < 48):
        desired = min(desired, 0.30)
    return max(spec["min_charge"], desired)


def fire_control(me, options, focused, evasive, hp_fraction):
    if not options:
        return None, [False, False]
    torso = me["stats"]
    actual_yaw, actual_pitch = me["aim"]
    commands = [(shot[0], shot[1]) for _, _, _, shot in options]
    if len(options) == 2:
        a, b = options[0][3], options[1][3]
        yaw_difference = angle_delta(b[0], a[0])
        if abs(yaw_difference) <= a[2] + b[2] and abs(a[1] - b[1]) <= a[3] + b[3]:
            fraction = a[2] / max(0.0001, a[2] + b[2])
            overlap_pitch = (a[1] * b[3] + b[1] * a[3]) / (a[3] + b[3])
            commands.append((a[0] + yaw_difference * fraction, overlap_pitch))

    best = (-math.inf, None, None)
    for yaw, raw_pitch in commands:
        pitch = clamp(raw_pitch, torso["min_pitch"], torso["max_pitch"])
        yaw_turn = angle_delta(yaw, actual_yaw)
        pitch_turn = pitch - actual_pitch
        score = -12 * abs(yaw_turn) - 4 * abs(pitch_turn)
        fire = [False, False]
        for value, slot, weapon, shot in options:
            wanted_yaw, wanted_pitch, tol_yaw, tol_pitch, _, meters = shot
            kind, cooldown = weapon["kind"], weapon.get("cooldown", 0)
            charge = weapon.get("charge", 0)
            shared = abs(angle_delta(yaw, wanted_yaw)) <= tol_yaw and abs(pitch - wanted_pitch) <= tol_pitch
            if shared:
                score += value * (0.23 if not charge else 0.14)
            turn_time = 0.075 if cooldown > 0.055 and kind != "laser" else 0.038
            future_yaw = actual_yaw + clamp(yaw_turn, -torso["turn_rate"] * turn_time, torso["turn_rate"] * turn_time)
            future_pitch = actual_pitch + clamp(pitch_turn, -torso["pitch_rate"] * turn_time, torso["pitch_rate"] * turn_time)
            aligned = shared and abs(angle_delta(future_yaw, wanted_yaw)) < tol_yaw + 0.008
            aligned = aligned and abs(future_pitch - wanted_pitch) < tol_pitch + 0.008
            if kind == "laser":
                spec = weapon["stats"]
                desired = charge_needed(me, focused, spec, evasive, hp_fraction, meters)
                if charge:
                    timeout = spec["charge"] + min(0.85, spec["hold_s"] * 0.7)
                    fire[slot] = charge < timeout and (charge < desired - 0.035 or not aligned)
                    if not fire[slot] and aligned:
                        score += value * 0.94
                    elif shared and charge > desired - 0.11:
                        score += value * 0.24
                elif cooldown <= 0.07 and shared and abs(yaw_turn) < 0.54:
                    fire[slot] = not evasive or meters > 45 or hp_fraction > 0.45
                    if fire[slot]:
                        score += value * 0.32
            elif aligned and cooldown < 0.105:
                fire[slot] = kind != "gun" or (weapon.get("ammo", 0) > 0 and weapon.get("reload", 0) < 0.05)
                if fire[slot]:
                    score += value * 0.70
        if score > best[0]:
            best = (score, [yaw, pitch], fire)
    return best[1], best[2]
