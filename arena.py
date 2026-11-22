"""Static collision queries and bounded route searches for the arena grid."""

from array import array
import heapq
import json
import math
from pathlib import Path
import sys
from time import perf_counter


def inside(x, y, polygon):
    crossing = False
    x0, y0 = polygon[-2:]
    for i in range(0, len(polygon), 2):
        x1, y1 = polygon[i], polygon[i + 1]
        if (y0 > y) != (y1 > y):
            crossing ^= x < (x1 - x0) * (y - y0) / (y1 - y0) + x0
        x0, y0 = x1, y1
    return crossing


class Arena:
    cell = 4.0

    def __init__(self, folder, mech):
        directory = Path(folder) / "v2"
        world = json.loads((directory / "world.json").read_text(encoding="utf-8"))
        terrain = world["terrain"]
        self.origin = (terrain["minX"], terrain["minY"])
        self.tx, self.ty = terrain["nx"], terrain["ny"]
        self.step = terrain["step"]
        self.scale = terrain.get("scale", 0.01)
        self.heights = array("h", (directory / terrain.get("file", "terrain.i16")).read_bytes())
        if sys.byteorder != "little":
            self.heights.byteswap()
        bound = world["bounds"]
        self.x0, self.y0 = bound["minX"], bound["minY"]
        self.x1, self.y1 = bound["maxX"], bound["maxY"]
        self.nx = math.ceil((self.x1 - self.x0) / self.cell)
        self.ny = math.ceil((self.y1 - self.y0) / self.cell)
        count = self.nx * self.ny
        self.columns = [[] for _ in range(count)]
        prisms = [(p["base"], p["top"], p["ring"], p["holes"]) for p in world["prisms"]]
        for prism in prisms:
            _, _, ring, _ = prism
            left, right = min(ring[::2]), max(ring[::2])
            bottom, top = min(ring[1::2]), max(ring[1::2])
            ix0, iy0 = self.indices(left, bottom)
            ix1, iy1 = self.indices(right, top)
            for iy in range(iy0, iy1 + 1):
                for ix in range(ix0, ix1 + 1):
                    self.columns[iy * self.nx + ix].append(prism)

        self.ground = array("f", [0.0]) * count
        clearance = bytearray([8]) * count
        height = mech.get("height", 9.0)
        step_up = mech.get("step_up", 1.2)
        for n in range(count):
            x, y = self.center(n)
            z = self.terrain(x, y)
            roof = z
            for base, top, ring, holes in self.columns[n]:
                if inside(x, y, ring) and not any(inside(x, y, h) for h in holes):
                    if base <= z + height + 1.0 and top > z + step_up:
                        clearance[n] = 0
                    elif base <= z + step_up:
                        roof = max(roof, top)
            self.ground[n] = roof
            if not self.in_bounds(x, y, 3):
                clearance[n] = 0

        for i in range(count):
            if i >= self.nx:
                clearance[i] = min(clearance[i], clearance[i - self.nx] + 1)
            if i % self.nx:
                clearance[i] = min(clearance[i], clearance[i - 1] + 1)
        for i in range(count - 1, -1, -1):
            if i < count - self.nx:
                clearance[i] = min(clearance[i], clearance[i + self.nx] + 1)
            if i % self.nx < self.nx - 1:
                clearance[i] = min(clearance[i], clearance[i + 1] + 1)
        self.clearance = clearance
        # Each match uses the same geometry throughout. Pay for building the graph
        # during the hello budget so a long detour can fit within a decision tick.
        self.centers = [self.center(i) for i in range(count)]
        self.edges = [[] for _ in range(count)]
        offsets = ((-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1))
        for i, space in enumerate(clearance):
            if not space:
                continue
            y, x = divmod(i, self.nx)
            for dx, dy in offsets:
                if not (0 <= x+dx < self.nx and 0 <= y+dy < self.ny):
                    continue
                j = i + dx + dy*self.nx
                width = min(space, clearance[j])
                if dx and dy:
                    width = min(width, clearance[i+dx], clearance[i+dy*self.nx])
                if width and abs(self.ground[j]-self.ground[i]) <= 3.4:
                    length = math.sqrt(2) if dx and dy else 1.0
                    self.edges[i].append((j, width, length, length+0.32/clearance[j]))
        self.landmarks = []
        self.landmark_paths = []
        open_cells = [i for i, space in enumerate(clearance) if space >= 2]
        if open_cells:
            pivot = self.nearest((self.x0+self.x1)/2, (self.y0+self.y1)/2, 2)
            if clearance[pivot] < 2:
                pivot = open_cells[0]
            separation = [math.inf] * count
            for _ in range(6):
                distances = [math.inf] * count
                toward = array("i", [-1]) * count
                distances[pivot] = 0.0
                heap = [(0.0,pivot)]
                while heap:
                    cost, vertex = heapq.heappop(heap)
                    if cost != distances[vertex]:
                        continue
                    for neighbor, width, length, _ in self.edges[vertex]:
                        candidate = cost + length
                        if width >= 2 and candidate < distances[neighbor]:
                            distances[neighbor] = candidate
                            toward[neighbor] = vertex
                            heapq.heappush(heap, (candidate,neighbor))
                self.landmarks.append(array("f", distances))
                self.landmark_paths.append(toward)
                reachable = [i for i in open_cells if distances[i] < math.inf]
                for i in reachable:
                    separation[i] = min(separation[i], distances[i])
                pivot = max(reachable, key=separation.__getitem__)

    def indices(self, x, y):
        return (
            max(0, min(self.nx - 1, int((x - self.x0) / self.cell))),
            max(0, min(self.ny - 1, int((y - self.y0) / self.cell))),
        )

    def key(self, x, y):
        ix, iy = self.indices(x, y)
        return iy * self.nx + ix

    def center(self, index):
        iy, ix = divmod(index, self.nx)
        return self.x0 + (ix + 0.5) * self.cell, self.y0 + (iy + 0.5) * self.cell

    def in_bounds(self, x, y, margin=0):
        return self.x0 + margin <= x <= self.x1 - margin and self.y0 + margin <= y <= self.y1 - margin

    def terrain(self, x, y):
        gx = max(0.0, min(self.tx - 1.001, (x - self.origin[0]) / self.step))
        gy = max(0.0, min(self.ty - 1.001, (y - self.origin[1]) / self.step))
        ix, iy = int(gx), int(gy)
        fx, fy = gx - ix, gy - iy
        row = self.tx
        i = iy * row + ix
        h = self.heights
        lower = h[i] * (1 - fx) + h[i + 1] * fx
        upper = h[i + row] * (1 - fx) + h[i + row + 1] * fx
        return (lower * (1 - fy) + upper * fy) * self.scale

    def walkable(self, x, y, z, height):
        if not self.in_bounds(x, y, 1):
            return False
        if self.terrain(x, y) > z + 1.25:
            return False
        for base, top, ring, holes in self.columns[self.key(x, y)]:
            if z + 1.2 < top and base < z + height - 0.2 and inside(x, y, ring):
                if not any(inside(x, y, h) for h in holes):
                    return False
        return True

    def solid(self, x, y, z):
        for base, top, ring, holes in self.columns[self.key(x, y)]:
            if base <= z <= top and inside(x, y, ring) and not any(inside(x, y, h) for h in holes):
                return True
        return z <= self.terrain(x, y)

    def visible(self, start, end):
        dx, dy, dz = (end[i] - start[i] for i in range(3))
        steps = max(2, math.ceil(math.hypot(dx, dy) / 3.8))
        for step in range(1, steps):
            f = step / steps
            if self.solid(start[0] + dx * f, start[1] + dy * f, start[2] + dz * f):
                return False
        return True

    def direct(self, source, target, radius):
        dx, dy = target[0] - source[0], target[1] - source[1]
        steps = max(1, math.ceil(math.hypot(dx, dy) / (self.cell * 0.8)))
        previous = self.key(*source)
        minimum = self.route_clearance(radius)
        for step in range(1, steps + 1):
            i = self.key(source[0] + dx * step / steps, source[1] + dy * step / steps)
            if self.clearance[i] < minimum or abs(self.ground[i] - self.ground[previous]) > 3.4:
                return False
            previous = i
        return True

    def nearest(self, x, y, minimum=1, max_ring=12):
        ix, iy = self.indices(x, y)
        n = iy * self.nx + ix
        if self.clearance[n] >= minimum:
            return n
        for ring in range(1, max_ring + 1):
            candidates = []
            for y2 in range(max(0, iy - ring), min(self.ny, iy + ring + 1)):
                for x2 in range(max(0, ix - ring), min(self.nx, ix + ring + 1)):
                    i = y2 * self.nx + x2
                    if max(abs(x2 - ix), abs(y2 - iy)) == ring and self.clearance[i] >= minimum:
                        a, b = self.center(i)
                        candidates.append(((a - x) ** 2 + (b - y) ** 2, i))
            if candidates:
                return min(candidates)[1]
        return n

    def route_clearance(self, radius):
        return max(1, math.ceil((radius + 1.8) / self.cell))

    def landmark_detour(self, start, finish):
        """Use a shared branch of a shortest-path tree if this tick's A* is unfinished."""
        best = None
        best_length = math.inf
        for distance, toward in zip(self.landmarks, self.landmark_paths):
            if not math.isfinite(distance[start]) or not math.isfinite(distance[finish]):
                continue
            a, b = start, finish
            prefix, suffix = [], []
            while a != b:
                if distance[a] >= distance[b]:
                    prefix.append(a)
                    a = toward[a]
                else:
                    suffix.append(b)
                    b = toward[b]
            length = distance[start] + distance[finish] - 2*distance[a]
            if length < best_length:
                best_length = length
                best = prefix + [a] + suffix[::-1]
        return best

    def circle_entry(self, source, center, circle_radius, radius):
        start = self.nearest(*source, self.route_clearance(radius), max_ring=4)
        guides = [(table, table[start]) for table in self.landmarks if math.isfinite(table[start])]
        if not guides or self.route_clearance(radius) != 2:
            return None
        best = math.inf
        selected = None
        angle = math.atan2(source[1]-center[1], source[0]-center[0])
        for fraction in (0.99, 0.76, 0.44, 0):
            for offset in range(24 if fraction else 1):
                theta = angle + math.tau*offset/24
                x = center[0]+circle_radius*fraction*math.cos(theta)
                y = center[1]+circle_radius*fraction*math.sin(theta)
                point = self.key(x,y)
                if self.clearance[point] < 2 or not math.isfinite(guides[0][0][point]):
                    continue
                target = self.centers[point]
                if math.dist(target,center)>circle_radius:
                    continue
                lower = max(abs(table[point]-d) for table,d in guides) * self.cell
                score = max(lower,math.dist(source,target)) + 2.0/self.clearance[point]
                if score < best:
                    selected,best = target,score
        return selected

    def route(self, source, goal, radius, stop_at, goal_radius=0.0):
        """Return a short list of waypoints, even if this tick only finds a partial path."""
        minimum = self.route_clearance(radius)
        start = self.nearest(*source, minimum, max_ring=4)
        finish = self.nearest(*goal, minimum) if not goal_radius else None
        finish_x, finish_y = self.center(finish) if finish is not None else goal

        guides = [(table, table[finish]) for table in self.landmarks
                  if finish is not None and minimum >= 2 and math.isfinite(table[finish])
                  and math.isfinite(table[start])]

        def estimate(index):
            x, y = self.centers[index]
            remaining = math.hypot(x - finish_x, y - finish_y) - goal_radius
            h = max(0, remaining / self.cell)
            for table, end in guides:
                h = max(h, abs(table[index]-end))
            return h

        open_set = [(estimate(start), 0, start)]
        distance = {start: 0}
        parent = {}
        best = start
        best_h = estimate(start)
        for count in range(3500):
            if not open_set or (count % 64 == 0 and perf_counter() > stop_at):
                break
            _, cost, i = heapq.heappop(open_set)
            if cost != distance[i]:
                continue
            h = estimate(i)
            if h < best_h:
                best_h, best = h, i
            if i == finish or (goal_radius and h == 0):
                break
            for j, width, _, weight in self.edges[i]:
                if width < minimum:
                    continue
                new = cost + weight
                if new < distance.get(j, math.inf):
                    parent[j], distance[j] = i, new
                    heapq.heappush(open_set, (new + 1.45 * estimate(j), new, j))
        if minimum == 2 and finish is not None and best != finish:
            detour = self.landmark_detour(start, finish)
            if detour:
                path = [self.centers[i] for i in detour]
                if start == self.key(*source):
                    path = path[1:]
                if finish == self.key(*goal):
                    path.append(goal)
                return path
        if best == start and open_set and open_set[0][2] in parent:
            best = open_set[0][2]
        path = []
        i = best
        while i != start:
            path.append(self.center(i))
            i = parent[i]
        if start != self.key(*source) or not path:
            path.append(self.center(start))
        path.reverse()
        if best == finish and finish == self.key(*goal):
            path.append(goal)
        return path
