"""证券结算与企业行动处理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Tuple

from .domain import DEFAULT_ACCOUNT, Conflict, ValidationError, choice, integer, number, optional_text, text


INITIAL_STATE = "captured"
CREATE_ROLES = {'trader'}
ACTION_ROLES = {
    'apply_corporate': {'corporate_actions'},
    'approve': {'settlement_officer'},
    'settle': {'settlement_officer'},
    'fail': {'settlement_officer'},
    'reverse': {'corporate_actions', 'settlement_officer'},
}
BATCH_ROLES = {'settlement_officer'}
CORPORATE_PUBLISH_ROLES = {'corporate_actions'}
TRANSITIONS = {'apply_corporate': {'captured': 'adjusted'}, 'approve': {'captured': 'approved', 'adjusted': 'approved'}, 'settle': {'approved': 'settled'}, 'fail': {'approved': 'failed'}, 'reverse': {'settled': 'reversed', 'failed': 'reversed'}}
CORPORATE_ACTIONS = ["none", "split", "dividend", "merger"]


def side_sign(side: str) -> int:
    return 1 if side == "buy" else -1


def compute_adjustments(quantity: int, price: float, side: str, corporate_action: str, ratio: float) -> Dict[str, Any]:
    """按给定公司行动版本参数计算调整后数量/价格与现金权益。"""
    adjusted_quantity = int(quantity)
    adjusted_price = round(float(price), 4)
    cash_entitlement: Any = None
    if corporate_action == "split":
        adjusted_quantity = int(float(quantity) * float(ratio))
        adjusted_price = round(float(price) / float(ratio), 4)
    elif corporate_action == "dividend":
        cash_entitlement = round(float(quantity) * float(ratio), 2)
    return {
        "adjusted_quantity": adjusted_quantity,
        "adjusted_price": adjusted_price,
        "cash_entitlement": cash_entitlement,
    }


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        all_roles.update(BATCH_ROLES)
        all_roles.update(CORPORATE_PUBLISH_ROLES)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_batch(self, role: str) -> bool:
        return role == "admin" or role in BATCH_ROLES

    def role_can_publish_corporate(self, role: str) -> bool:
        return role == "admin" or role in CORPORATE_PUBLISH_ROLES

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "instrument")
        choice(p, "side", ["buy", "sell"])
        integer(p, "quantity", 1)
        number(p, "price", 0.01)
        number(p, "fees", 0)
        choice(p, "currency", ["CNY", "USD", "HKD"])
        integer(p, "settlement_day", 0)
        choice(p, "corporate_action", CORPORATE_ACTIONS)
        number(p, "action_ratio", 0.01)
        return p

    def validate_corporate_revision(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        text(p, "instrument")
        choice(p, "corporate_action", ["split", "dividend", "merger"])
        ratio = number(p, "action_ratio", 0.01)
        optional_text(p, "remark")
        return {"instrument": p["instrument"], "corporate_action": p["corporate_action"], "action_ratio": ratio, "remark": p.get("remark", "")}

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["account"] = optional_text(p, "account", DEFAULT_ACCOUNT) or DEFAULT_ACCOUNT
        gross = float(p["quantity"]) * float(p["price"])
        fee = float(p["fees"])
        p["gross_amount"] = round(gross, 2)
        p["net_amount"] = round(gross + fee if p["side"] == "buy" else gross - fee, 2)
        adjustments = compute_adjustments(p["quantity"], p["price"], p["side"], p["corporate_action"], p["action_ratio"])
        p["adjusted_quantity"] = adjustments["adjusted_quantity"]
        p["adjusted_price"] = adjustments["adjusted_price"]
        if adjustments["cash_entitlement"] is not None:
            p["cash_entitlement"] = adjustments["cash_entitlement"]
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"settled", "reversed"} and item["payload"].get("instrument") == payload.get("instrument") and item["payload"].get("settlement_day") == payload.get("settlement_day"):
                ip = item["payload"]
                if ip.get("side") == payload.get("side") and ip.get("quantity") == payload.get("quantity") and ip.get("price") == payload.get("price") and ip.get("action_version", 0) == payload.get("action_version", 0):
                    raise Conflict("疑似重复结算指令")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "apply_corporate":
            if p["corporate_action"] == "none":
                raise ValidationError("没有待处理的公司行动")
            changes["corporate_applied"] = True
            changes["effective_quantity"] = p["adjusted_quantity"]
            changes["effective_price"] = p["adjusted_price"]
            summary = "公司行动已应用"
        elif action == "approve":
            changes["approved_amount"] = p["net_amount"]
            summary = "结算指令复核通过"
        elif action == "settle":
            delivered = integer(data, "delivered_quantity", 0)
            paid = number(data, "cash_paid", 0)
            required_quantity = int(p.get("effective_quantity", p["quantity"]))
            if delivered != required_quantity:
                raise ValidationError("交收证券数量不匹配")
            if paid < float(p["net_amount"]):
                raise ValidationError("交收资金不足")
            changes["delivered_quantity"] = delivered
            changes["cash_paid"] = paid
            summary = "交收完成"
        elif action == "fail":
            changes["fail_reason"] = text(data, "fail_reason")
            summary = "交收失败"
        elif action == "reverse":
            changes["reverse_reason"] = text(data, "reverse_reason")
            summary = "交收冲正"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # ---- 公司行动版本 -------------------------------------------------

    def bind_corporate_version(self, prepared: Dict[str, Any], latest: Dict[str, Any]) -> Dict[str, Any]:
        """创建指令时锁定当时的公司行动版本；比例漂移则拒绝，防止版本混用。"""
        p = dict(prepared)
        if p["corporate_action"] == "none":
            p["action_version"] = 0
            return p
        if latest is None:
            p["action_version"] = 1
            return p
        if round(float(latest["action_ratio"]), 6) != round(float(p["action_ratio"]), 6):
            raise Conflict(
                "公司行动比例已修正，请按最新版本重新提交",
                {"latest_version": latest["version"], "latest_ratio": latest["action_ratio"]},
            )
        p["action_version"] = int(latest["version"])
        return p

    def recompute_payload(self, payload: Dict[str, Any], ca_version: Dict[str, Any]) -> Dict[str, Any]:
        """按指定公司行动版本重算指令参数（未入批指令采用新版本）。"""
        p = dict(payload)
        ratio = float(ca_version["action_ratio"])
        adjustments = compute_adjustments(p["quantity"], p["price"], p["side"], p["corporate_action"], ratio)
        p["action_ratio"] = ratio
        p["action_version"] = int(ca_version["version"])
        p["adjusted_quantity"] = adjustments["adjusted_quantity"]
        p["adjusted_price"] = adjustments["adjusted_price"]
        if adjustments["cash_entitlement"] is not None:
            p["cash_entitlement"] = adjustments["cash_entitlement"]
        if p.get("corporate_applied"):
            p["effective_quantity"] = adjustments["adjusted_quantity"]
            p["effective_price"] = adjustments["adjusted_price"]
        return p

    # ---- 净额批次 -----------------------------------------------------

    def net_snapshot(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """按指令当前锁定的公司行动版本计算净收付数量快照。"""
        p = record["payload"]
        sign = side_sign(p["side"])
        quantity = int(p.get("effective_quantity", p.get("adjusted_quantity", p["quantity"])))
        cash = round(float(p["net_amount"]), 2)
        if p.get("cash_entitlement") is not None:
            # 股利现金与应付/应收同向：买方付券款收股利，卖方反之
            cash = round(cash - sign * float(p["cash_entitlement"]), 2)
        return {
            "record_id": int(record["id"]),
            "reference": record.get("reference", ""),
            "side": p["side"],
            "record_version": int(record["version"]),
            "action_version": int(p.get("action_version", 0) or 0),
            "action_ratio": float(p.get("action_ratio", 0.0)),
            "settlement_quantity": quantity,
            "signed_quantity": sign * quantity,
            "net_cash_amount": sign * cash,
        }

    def batch_totals(self, snapshots: List[Dict[str, Any]]) -> Dict[str, Any]:
        net_qty = sum(int(item["signed_quantity"]) for item in snapshots)
        net_cash = round(sum(float(item["net_cash_amount"]) for item in snapshots), 2)
        return {
            "net_quantity": net_qty,
            "net_cash_amount": net_cash,
            "net_direction": ("buy" if net_qty > 0 else "sell" if net_qty < 0 else "flat"),
            "instruction_count": len(snapshots),
        }
