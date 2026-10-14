"""Slider path approximation, ported from osu-framework's PathApproximator and osu!'s SliderPath."""
import bisect
import math

BEZIER_TOLERANCE = 0.25
CATMULL_DETAIL = 50
CIRCULAR_ARC_TOLERANCE = 0.1
FLOAT_EPSILON = 1e-3


def Sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def Length(v):
    return math.hypot(v[0], v[1])


def BezierIsFlatEnough(points):
    limit = BEZIER_TOLERANCE * BEZIER_TOLERANCE * 4
    for i in range(1, len(points) - 1):
        dx = points[i - 1][0] - 2 * points[i][0] + points[i + 1][0]
        dy = points[i - 1][1] - 2 * points[i][1] + points[i + 1][1]
        if dx * dx + dy * dy > limit:
            return False
    return True


def BezierSubdivide(points):
    count = len(points)
    midpoints = list(points)
    left = [None] * count
    right = [None] * count
    for i in range(count):
        left[i] = midpoints[0]
        right[count - i - 1] = midpoints[count - i - 1]
        for j in range(count - i - 1):
            midpoints[j] = ((midpoints[j][0] + midpoints[j + 1][0]) / 2, (midpoints[j][1] + midpoints[j + 1][1]) / 2)
    return left, right


def BezierApproximate(points, output):
    count = len(points)
    left, right = BezierSubdivide(points)
    joined = left + right[1:]
    output.append(points[0])
    for i in range(1, count - 1):
        index = 2 * i
        a, b, c = joined[index - 1], joined[index], joined[index + 1]
        output.append((0.25 * (a[0] + 2 * b[0] + c[0]), 0.25 * (a[1] + 2 * b[1] + c[1])))


def BezierToPiecewiseLinear(points):
    if len(points) < 2:
        return list(points)
    output = []
    to_flatten = [list(points)]
    while to_flatten:
        parent = to_flatten.pop()
        if BezierIsFlatEnough(parent):
            BezierApproximate(parent, output)
            continue
        left, right = BezierSubdivide(parent)
        to_flatten.append(right)
        to_flatten.append(left)
    output.append(points[-1])
    return output


def CatmullFindPoint(v1, v2, v3, v4, t):
    t2 = t * t
    t3 = t * t2
    return tuple(
        0.5 * (2 * v2[i] + (-v1[i] + v3[i]) * t + (2 * v1[i] - 5 * v2[i] + 4 * v3[i] - v4[i]) * t2
               + (-v1[i] + 3 * v2[i] - 3 * v3[i] + v4[i]) * t3)
        for i in range(2)
    )


def CatmullToPiecewiseLinear(points):
    result = []
    for i in range(len(points) - 1):
        v1 = points[i - 1] if i > 0 else points[i]
        v2 = points[i]
        v3 = points[i + 1] if i < len(points) - 1 else (2 * v2[0] - v1[0], 2 * v2[1] - v1[1])
        v4 = points[i + 2] if i < len(points) - 2 else (2 * v3[0] - v2[0], 2 * v3[1] - v2[1])
        for c in range(CATMULL_DETAIL):
            result.append(CatmullFindPoint(v1, v2, v3, v4, c / CATMULL_DETAIL))
            result.append(CatmullFindPoint(v1, v2, v3, v4, (c + 1) / CATMULL_DETAIL))
    return result


def CircularArcProperties(points):
    a, b, c = points
    if abs((b[1] - a[1]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[1] - a[1])) <= FLOAT_EPSILON:
        return None
    d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
    a_sq = a[0] ** 2 + a[1] ** 2
    b_sq = b[0] ** 2 + b[1] ** 2
    c_sq = c[0] ** 2 + c[1] ** 2
    centre = (
        (a_sq * (b[1] - c[1]) + b_sq * (c[1] - a[1]) + c_sq * (a[1] - b[1])) / d,
        (a_sq * (c[0] - b[0]) + b_sq * (a[0] - c[0]) + c_sq * (b[0] - a[0])) / d,
    )
    d_a = Sub(a, centre)
    d_c = Sub(c, centre)
    radius = Length(d_a)
    theta_start = math.atan2(d_a[1], d_a[0])
    theta_end = math.atan2(d_c[1], d_c[0])
    while theta_end < theta_start:
        theta_end += 2 * math.pi
    direction = 1
    theta_range = theta_end - theta_start
    ortho_a_to_c = (c[1] - a[1], -(c[0] - a[0]))
    if ortho_a_to_c[0] * (b[0] - a[0]) + ortho_a_to_c[1] * (b[1] - a[1]) < 0:
        direction = -1
        theta_range = 2 * math.pi - theta_range
    return centre, radius, theta_start, theta_range, direction


def CircularArcPointCount(radius, theta_range):
    if 2 * radius <= CIRCULAR_ARC_TOLERANCE:
        return 2
    return max(2, math.ceil(theta_range / (2 * math.acos(1 - CIRCULAR_ARC_TOLERANCE / radius))))


def CircularArcToPiecewiseLinear(points):
    properties = CircularArcProperties(points)
    if properties is None:
        return BezierToPiecewiseLinear(points)
    centre, radius, theta_start, theta_range, direction = properties
    amount = CircularArcPointCount(radius, theta_range)
    output = []
    for i in range(amount):
        theta = theta_start + direction * (i / (amount - 1)) * theta_range
        output.append((centre[0] + math.cos(theta) * radius, centre[1] + math.sin(theta) * radius))
    return output


def SubPath(points, path_type):
    if path_type == "L":
        return list(points)
    if path_type == "P" and len(points) == 3:
        properties = CircularArcProperties(points)
        if properties is not None and CircularArcPointCount(properties[1], properties[3]) < 1000:
            return CircularArcToPiecewiseLinear(points)
    if path_type == "C":
        return CatmullToPiecewiseLinear(points)
    return BezierToPiecewiseLinear(points)


class SliderPath:
    def __init__(self, control_points, expected_distance=None):
        """control_points: list of ((x, y), type or None), positions relative to the slider head."""
        self.path = []
        self.cumulative_length = []
        self.CalculatePath(control_points)
        self.CalculateLength(expected_distance)

    def CalculatePath(self, control_points):
        if not control_points:
            return
        vertices = [position for position, _ in control_points]
        start = 0
        for i, (_, point_type) in enumerate(control_points):
            if point_type is None and i < len(control_points) - 1:
                continue
            segment = vertices[start:i + 1]
            segment_type = control_points[start][1] or "L"
            if len(segment) == 1:
                self.path.append(segment[0])
            elif len(segment) > 1:
                sub_path = SubPath(segment, segment_type)
                skip_first = bool(self.path) and bool(sub_path) and self.path[-1] == sub_path[0]
                self.path.extend(sub_path[1:] if skip_first else sub_path)
            start = i

    def CalculateLength(self, expected_distance):
        length = 0.0
        self.cumulative_length = [0.0]
        for i in range(len(self.path) - 1):
            length += Length(Sub(self.path[i + 1], self.path[i]))
            self.cumulative_length.append(length)

        if expected_distance is None or length == expected_distance:
            return
        if len(self.path) >= 2 and self.path[-1] == self.path[-2] and expected_distance > length:
            self.cumulative_length.append(length)
            return

        self.cumulative_length.pop()
        end_index = len(self.path) - 1
        if length > expected_distance:
            while self.cumulative_length and self.cumulative_length[-1] >= expected_distance:
                self.cumulative_length.pop()
                self.path.pop(end_index)
                end_index -= 1
        if end_index <= 0:
            self.cumulative_length.append(0.0)
            return

        direction = Sub(self.path[end_index], self.path[end_index - 1])
        magnitude = Length(direction)
        unit = (direction[0] / magnitude, direction[1] / magnitude) if magnitude > 0 else (0.0, 0.0)
        remaining = expected_distance - self.cumulative_length[-1]
        previous = self.path[end_index - 1]
        self.path[end_index] = (previous[0] + unit[0] * remaining, previous[1] + unit[1] * remaining)
        self.cumulative_length.append(expected_distance)

    @property
    def distance(self):
        return self.cumulative_length[-1] if self.cumulative_length else 0.0

    def PositionAt(self, progress):
        if not self.path:
            return (0.0, 0.0)
        d = min(max(progress, 0.0), 1.0) * self.distance
        i = bisect.bisect_left(self.cumulative_length, d)
        if i <= 0:
            return self.path[0]
        if i >= len(self.path):
            return self.path[-1]
        p0, p1 = self.path[i - 1], self.path[i]
        d0, d1 = self.cumulative_length[i - 1], self.cumulative_length[i]
        if abs(d0 - d1) <= FLOAT_EPSILON:
            return p0
        w = (d - d0) / (d1 - d0)
        return (p0[0] + (p1[0] - p0[0]) * w, p0[1] + (p1[1] - p0[1]) * w)
