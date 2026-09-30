"""证券结算与企业行动处理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "captured"
CREATE_ROLES = {'trader'}
ACTION_ROLES = {'apply_corporate': {'corporate_actions'}, 'approve': {'settlement_officer'}, 'settle': {'settlement_officer'}, 'fail': {'settlement_officer'}, 'reverse': {'corporate_actions', 'settlement_officer'}}
TRANSITIONS = {'apply_corporate': {'captured': 'adjusted'}, 'approve': {'captured': 'approved', 'adjusted': 'approved'}, 'settle': {'approved': 'settled'}, 'fail': {'approved': 'failed'}, 'reverse': {'settled': 'reversed', 'failed': 'reversed'}}

CURRENCIES = ["CNY", "USD", "HKD"]
CA_ACTION_TYPES = ["none", "split", "dividend", "merger"]
BATCHABLE_STATES = {"captured", "adjusted", "approved"}
GENERATE_BATCH_ROLES = {'settlement_officer', 'settlement_supervisor'}
CONFIRM_BATCH_ROLES = {'settlement_supervisor'}
REGISTER_CA_ROLES = {'corporate_actions'}
TERMINAL_GUARD_ACTIONS = {"settle", "fail", "reverse"}


def effective_position(quantity: int, action_type: str, ratio: float) -> Tuple[int, float]:
    """按公司行动参数折算证券数量与现金权益。"""
    if action_type in ("split", "merger"):
        return int(quantity * ratio), 0.0
    if action_type == "dividend":
        return int(quantity), round(quantity * ratio, 2)
    return int(quantity), 0.0


def batch_item(payload: Dict[str, Any], ca_version: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把结算指令折算成批次明细：采用的公司行动版本随明细冻结，净收付数量正收负付。"""
    action_type = ca_version["action_type"] if ca_version else "none"
    ratio = float(ca_version["ratio"]) if ca_version else 1.0
    adjusted, entitlement = effective_position(int(payload["quantity"]), action_type, ratio)
    base = float(payload["net_amount"])
    cash = -base if payload["side"] == "buy" else base
    cash += entitlement
    return {
        "instrument": payload["instrument"],
        "side": payload["side"],
        "ca_version": int(ca_version["version"]) if ca_version else None,
        "net_quantity": adjusted if payload["side"] == "buy" else -adjusted,
        "net_amount": round(cash, 2),
    }


def net_totals(items: List[Dict[str, Any]]) -> Tuple[Dict[str, int], float]:
    """按标的汇总净证券数量，并计算批次净现金（正收负付）。"""
    quantities: Dict[str, int] = {}
    cash = 0.0
    for item in items:
        quantities[item["instrument"]] = quantities.get(item["instrument"], 0) + int(item["net_quantity"])
        cash += float(item["net_amount"])
    return quantities, round(cash, 2)


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES) | GENERATE_BATCH_ROLES | CONFIRM_BATCH_ROLES | REGISTER_CA_ROLES
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_generate_batch(self, role: str) -> bool:
        return role == "admin" or role in GENERATE_BATCH_ROLES

    def role_can_confirm_batch(self, role: str) -> bool:
        return role == "admin" or role in CONFIRM_BATCH_ROLES

    def role_can_register_ca(self, role: str) -> bool:
        return role == "admin" or role in REGISTER_CA_ROLES

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "instrument")
        choice(p, "side", ["buy", "sell"])
        integer(p, "quantity", 1)
        number(p, "price", 0.01)
        number(p, "fees", 0)
        choice(p, "currency", CURRENCIES)
        integer(p, "settlement_day", 0)
        choice(p, "corporate_action", CA_ACTION_TYPES)
        number(p, "action_ratio", 0.01)
        p["account"] = optional_text(p, "account", "DEFAULT") or "DEFAULT"
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        gross = float(p["quantity"]) * float(p["price"])
        fee = float(p["fees"])
        p["gross_amount"] = round(gross, 2)
        p["net_amount"] = round(gross + fee if p["side"] == "buy" else gross - fee, 2)
        p["adjusted_quantity"] = p["quantity"]
        p["adjusted_price"] = p["price"]
        if p["corporate_action"] == "split":
            p["adjusted_quantity"] = int(float(p["quantity"]) * float(p["action_ratio"]))
            p["adjusted_price"] = round(float(p["price"]) / float(p["action_ratio"]), 4)
        elif p["corporate_action"] == "dividend":
            p["cash_entitlement"] = round(float(p["quantity"]) * float(p["action_ratio"]), 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"settled", "reversed"} and item["payload"].get("instrument") == payload.get("instrument") and item["payload"].get("settlement_day") == payload.get("settlement_day"):
                if item["payload"].get("side") == payload.get("side") and item["payload"].get("quantity") == payload.get("quantity") and item["payload"].get("price") == payload.get("price"):
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

    def validate_ca_registration(self, data: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(data or {})
        instrument = text(data, "instrument")
        action_type = choice(data, "action_type", CA_ACTION_TYPES)
        if action_type == "none":
            ratio_value = data.get("ratio", 1.0)
            ratio = 1.0 if ratio_value is None else number({"ratio": ratio_value}, "ratio", 0.01)
        else:
            ratio = number(data, "ratio", 0.01)
        return {"instrument": instrument, "action_type": action_type, "ratio": ratio, "note": optional_text(data, "note")}

    def validate_batch_request(self, data: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(data or {})
        return {
            "batch_no": text(data, "batch_no"),
            "account": text(data, "account"),
            "currency": choice(data, "currency", CURRENCIES),
            "settlement_day": integer(data, "settlement_day", 0),
        }
