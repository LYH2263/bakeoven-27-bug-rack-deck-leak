from app.services.oven_engine import (
    Interval,
    Occupancy,
    OvenCapacity,
    RecipeDurations,
    build_occupancies,
    check_capacity,
    find_conflicts,
    next_free_window,
)


CAP_2_1 = OvenCapacity(rack_slots=2, hearth_slots=1)


def test_half_open_no_touch_conflict():
    a = Occupancy(1, Interval(30, 60), "bake", 2)
    b = Occupancy(1, Interval(0, 30), "bake", 1)
    assert find_conflicts([a], [b]) == []


def test_overlap_detected():
    recipe = RecipeDurations(20, 30)
    cand = build_occupancies(1, 9, 10, recipe)
    existing = [Occupancy(1, Interval(25, 40), "bake", 1)]
    assert find_conflicts(existing, cand)


def test_next_free_window_after_busy():
    existing = [
        Occupancy(1, Interval(0, 40), "ferment", 1),
        Occupancy(1, Interval(40, 70), "bake", 1),
    ]
    w = next_free_window(existing, 1, duration=30, search_from=0)
    assert w == Interval(70, 100)


def test_next_free_in_gap():
    existing = [
        Occupancy(1, Interval(0, 20), "bake", 1),
        Occupancy(1, Interval(80, 100), "bake", 2),
    ]
    w = next_free_window(existing, 1, duration=30, search_from=0)
    assert w == Interval(20, 50)


# —— 醒发架 / 炉膛 分开计容 ——
# 配方：发酵 30、烘烤 20。
#   A 00:00 起：酵[0,30) 烤[30,50)
#   B 00:20 起：酵[20,50) 烤[50,70)   仅发酵重叠，烘烤端点相接
#   C 00:10 起：酵[10,40) 烤[40,60)   烘烤重叠
RECIPE = RecipeDurations(30, 20)
A = build_occupancies(1, 101, 0, RECIPE)
B = build_occupancies(1, 102, 20, RECIPE)
C = build_occupancies(1, 103, 10, RECIPE)


def test_only_ferment_overlap_both_kept():
    # 醒发架 2：两批发酵重叠时架上正好 2，烘烤端点相接不算同时 → 通过
    assert check_capacity(A, B, CAP_2_1) == []


def test_bake_overlap_rejected_as_hearth_full():
    # 炉膛 1：C 与 A 烘烤在 [40,50) 同时为 2，后批被拒
    vs = check_capacity(A, C, CAP_2_1)
    assert len(vs) == 1
    v = vs[0]
    assert v.resource == "hearth"
    assert v.phase == "bake"
    assert v.limit == 1
    assert v.peak == 2
    assert v.start == 40 and v.end == 50
    assert v.rivals == (101,)


def test_rack_overflow_reported_as_rack_full():
    # 架 2：A、B 发酵已占满 [20,30)，再来一批发酵 D 同时刻 → 架满
    d = build_occupancies(1, 104, 15, RECIPE)  # 酵[15,45) 烤[45,65)
    vs = check_capacity(A + B, d, CAP_2_1)
    by_resource = {v.resource: v for v in vs}
    rack = by_resource["rack"]
    assert rack.phase == "ferment"
    assert rack.peak == 3
    assert set(rack.rivals) == {101, 102}
    # D 的烘烤[45,65) 与 A 烘烤[30,50) 在 [45,50) 叠到 2，膛 1 同样要报膛满
    hearth = by_resource["hearth"]
    assert hearth.peak == 2 and hearth.start == 45 and hearth.end == 50
    assert hearth.rivals == (101,)
    # 架超编更重，排前面
    assert vs[0].resource == "rack"


def test_ferment_and_bake_share_time_but_not_resource():
    # 架1膛1：B 的发酵[20,50) 与既有 A 的烘烤[30,50) 同时，但分属架/膛，互不挤占
    cap = OvenCapacity(rack_slots=1, hearth_slots=1)
    a_bake = [Occupancy(1, Interval(30, 50), "bake", 101)]
    assert check_capacity(a_bake, B, cap) == []


def test_half_open_endpoint_not_simultaneous():
    # A 烤[30,50)，B 烤[50,70)：50 分端点相接，膛仅 1 也不算同时
    a = [Occupancy(1, Interval(30, 50), "bake", 101)]
    b = [Occupancy(1, Interval(50, 70), "bake", 102)]
    assert check_capacity(a, b, CAP_2_1) == []


def test_both_limits_empty_falls_back_to_overlap():
    # 两项都留空：check_capacity 不设限，是否冲突退回纯时间重叠判定
    cap = OvenCapacity(rack_slots=None, hearth_slots=None)
    assert check_capacity(A, C, cap) == []
    assert find_conflicts(A, C)  # 酵/烤确有重叠，由 find_conflicts 拒绝


def test_only_one_limit_set_other_unrestricted():
    # 只设架1：发酵重叠被拒；不设膛，烘烤重叠不按容量拦
    cap = OvenCapacity(rack_slots=1, hearth_slots=None)
    vs = check_capacity(A, B, cap)
    assert len(vs) == 1 and vs[0].resource == "rack"
    a_bake = [Occupancy(1, Interval(30, 50), "bake", 101)]
    c_bake = [Occupancy(1, Interval(40, 60), "bake", 103)]
    assert check_capacity(a_bake, c_bake, cap) == []


def test_rack_does_not_count_bake_and_vice_versa():
    # 架1：只放烘烤段互不挤占架；膛1：只放发酵段互不挤占膛
    rack_only = OvenCapacity(rack_slots=1, hearth_slots=None)
    two_bakes = [Occupancy(1, Interval(0, 20), "bake", 201), Occupancy(1, Interval(10, 30), "bake", 202)]
    cand_bake = [Occupancy(1, Interval(15, 25), "bake", 203)]
    assert check_capacity(two_bakes, cand_bake, rack_only) == []
    hearth_only = OvenCapacity(rack_slots=None, hearth_slots=1)
    two_ferm = [Occupancy(1, Interval(0, 20), "ferment", 201), Occupancy(1, Interval(10, 30), "ferment", 202)]
    cand_ferm = [Occupancy(1, Interval(15, 25), "ferment", 203)]
    assert check_capacity(two_ferm, cand_ferm, hearth_only) == []


def test_zero_duration_phase_never_occupies():
    # 发酵 0 分钟（如布朗尼）：酵段为空，只占炉膛
    recipe = RecipeDurations(0, 30)
    occ = build_occupancies(1, 301, 0, recipe)
    assert [o.phase for o in occ] == ["ferment", "bake"]
    assert occ[0].interval.start == occ[0].interval.end
    cap = OvenCapacity(rack_slots=1, hearth_slots=1)
    other = [Occupancy(1, Interval(0, 30), "ferment", 302)]
    vs = check_capacity(other, occ, cap)
    assert all(v.resource != "rack" for v in vs)
