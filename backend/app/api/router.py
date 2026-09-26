from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Batch, ConflictLog, Oven, Product
from app.schemas.schemas import (
    BatchCreate,
    BatchOut,
    ConflictOut,
    GanttBlock,
    OvenOut,
    OvenUpdate,
    ProductOut,
    WindowOut,
)
from app.services.oven_engine import (
    Occupancy,
    OvenCapacity,
    RecipeDurations,
    build_occupancies,
    check_capacity,
    find_conflicts,
    next_free_window,
)

api_router = APIRouter()


def _recipe(p: Product) -> RecipeDurations:
    return RecipeDurations(p.ferment_min, p.bake_min)


def _capacity(o: Oven) -> OvenCapacity:
    return OvenCapacity(rack_slots=o.rack_slots, hearth_slots=o.hearth_slots)


def _fmt_min(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _rival_label(db: Session, batch_id: int) -> str:
    b = db.get(Batch, batch_id)
    return f"{b.code}(#{batch_id})" if b else f"批次#{batch_id}"


_PHASE_NAME = {"ferment": "发酵段", "bake": "烘烤段"}


def _violation_detail(db: Session, v) -> str:
    """架满/膛满文案：哪边满、上限、同时在占几批、对手批号、区间。"""
    resource_name = "醒发架" if v.resource == "rack" else "炉膛"
    unit = "格" if v.resource == "rack" else "盘"
    rivals = "、".join(_rival_label(db, rid) for rid in v.rivals) or "（无）"
    return (
        f"{resource_name}满：上限{v.limit}{unit}，"
        f"{_fmt_min(v.start)}–{_fmt_min(v.end)} 同时在占{v.peak}批"
        f"（对手 {rivals}）"
    )


def _all_occupancies(db: Session) -> list[Occupancy]:
    batches = db.scalars(select(Batch)).all()
    out: list[Occupancy] = []
    for b in batches:
        p = db.get(Product, b.product_id)
        if not p:
            continue
        out.extend(build_occupancies(b.oven_id, b.id, b.start_min, _recipe(p)))
    return out


def _batch_out(db: Session, b: Batch) -> BatchOut:
    p = db.get(Product, b.product_id)
    o = db.get(Oven, b.oven_id)
    ferment_end = b.start_min + (p.ferment_min if p else 0)
    bake_end = ferment_end + (p.bake_min if p else 0)
    return BatchOut(
        id=b.id,
        product_id=b.product_id,
        oven_id=b.oven_id,
        code=b.code,
        start_min=b.start_min,
        status=b.status,
        product_name=p.name if p else None,
        oven_label=o.label if o else None,
        ferment_end=ferment_end,
        bake_end=bake_end,
    )


@api_router.get("/health")
def health():
    return {"status": "ok"}


@api_router.get("/products", response_model=list[ProductOut])
def products(db: Session = Depends(get_db)):
    return db.scalars(select(Product).order_by(Product.id)).all()


@api_router.get("/ovens", response_model=list[OvenOut])
def ovens(db: Session = Depends(get_db)):
    return db.scalars(select(Oven).order_by(Oven.id)).all()


@api_router.patch("/ovens/{oven_id}", response_model=OvenOut)
def update_oven(oven_id: int, body: OvenUpdate, db: Session = Depends(get_db)):
    oven = db.get(Oven, oven_id)
    if not oven:
        raise HTTPException(404, "炉位不存在")
    data = body.model_dump(exclude_unset=True)
    if "rack_slots" in data:
        oven.rack_slots = data["rack_slots"]
    if "hearth_slots" in data:
        oven.hearth_slots = data["hearth_slots"]
    db.commit()
    db.refresh(oven)
    return oven


@api_router.get("/batches", response_model=list[BatchOut])
def batches(db: Session = Depends(get_db)):
    rows = db.scalars(select(Batch).order_by(Batch.start_min)).all()
    return [_batch_out(db, b) for b in rows]


@api_router.post("/batches", response_model=BatchOut)
def create_batch(body: BatchCreate, db: Session = Depends(get_db)):
    product = db.get(Product, body.product_id)
    oven = db.get(Oven, body.oven_id)
    if not product or not oven:
        raise HTTPException(404, "产品或炉位不存在")
    recipe = _recipe(product)
    candidates = build_occupancies(oven.id, -1, body.start_min, recipe)
    existing = _all_occupancies(db)
    code = body.code or f"BO-{body.start_min}"
    capacity = _capacity(oven)

    detail: str | None = None
    if capacity.rack_slots is None and capacity.hearth_slots is None:
        # 两项都留空：退回纯时间半开重叠互斥（贴边相接不算重叠）
        hits = find_conflicts(existing, candidates)
        if hits:
            ex, cand = hits[0]
            detail = (
                f"时间重叠：与{_rival_label(db, ex.batch_id)} 的{_PHASE_NAME[ex.phase]}在 "
                f"[{_fmt_min(cand.interval.start)},{_fmt_min(cand.interval.end)}) 重叠"
            )
    else:
        # 架只吃发酵、膛只吃烘烤；超了分别报“醒发架满 / 炉膛满”
        violations = check_capacity(existing, candidates, capacity)
        if violations:
            detail = "；".join(_violation_detail(db, v) for v in violations)

    if detail is not None:
        db.add(ConflictLog(batch_code=code, oven_id=oven.id, detail=detail[:240]))
        db.commit()
        raise HTTPException(409, detail)
    batch = Batch(
        product_id=product.id,
        oven_id=oven.id,
        code=code,
        start_min=body.start_min,
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return _batch_out(db, batch)


@api_router.get("/gantt", response_model=list[GanttBlock])
def gantt(db: Session = Depends(get_db)):
    blocks: list[GanttBlock] = []
    for b in db.scalars(select(Batch).order_by(Batch.start_min)).all():
        p = db.get(Product, b.product_id)
        o = db.get(Oven, b.oven_id)
        if not p or not o:
            continue
        for occ in build_occupancies(b.oven_id, b.id, b.start_min, _recipe(p)):
            blocks.append(
                GanttBlock(
                    batch_id=b.id,
                    code=b.code,
                    oven_id=o.id,
                    oven_label=o.label,
                    phase=occ.phase,
                    start_min=occ.interval.start,
                    end_min=occ.interval.end,
                )
            )
    return blocks


@api_router.get("/conflicts", response_model=list[ConflictOut])
def conflicts(db: Session = Depends(get_db)):
    rows = db.scalars(select(ConflictLog).order_by(ConflictLog.id.desc())).all()
    out: list[ConflictOut] = []
    for c in rows:
        o = db.get(Oven, c.oven_id)
        out.append(
            ConflictOut(
                id=c.id,
                batch_code=c.batch_code,
                oven_id=c.oven_id,
                oven_label=o.label if o else None,
                detail=c.detail,
                created_at=c.created_at,
            )
        )
    return out


@api_router.get("/windows", response_model=list[WindowOut])
def windows(product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "产品不存在")
    duration = product.ferment_min + product.bake_min
    existing = _all_occupancies(db)
    out: list[WindowOut] = []
    for oven in db.scalars(select(Oven).order_by(Oven.id)).all():
        w = next_free_window(existing, oven.id, duration, search_from=8 * 60, search_to=22 * 60)
        if w:
            out.append(
                WindowOut(
                    oven_id=oven.id,
                    oven_label=oven.label,
                    start_min=w.start,
                    end_min=w.end,
                    duration_min=duration,
                )
            )
    return out
