"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError, text
from .repository import BatchNoExists, Repository
from .rules import BATCHABLE_STATES, TERMINAL_GUARD_ACTIONS, DomainRules, batch_item, net_totals


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        if prepared["corporate_action"] != "none" and self.repository.latest_ca_version(prepared["instrument"]) is None:
            self.repository.insert_ca_version(prepared["instrument"], prepared["corporate_action"], float(prepared["action_ratio"]), "指令创建时补登", actor.user_id)
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        if action in TERMINAL_GUARD_ACTIONS and record_id in self.repository.locked_record_ids():
            raise Conflict("指令已入净额批次，禁止逐笔办理")
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---- 公司行动版本 ----

    def register_corporate_action(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        """登记公司行动新版本（交收前比例修正），并按新版本重算未入批指令。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_register_ca(actor.role):
            raise PermissionDenied("角色无权登记公司行动版本")
        validated = self.rules.validate_ca_registration(data)
        ca_version = self.repository.insert_ca_version(validated["instrument"], validated["action_type"], validated["ratio"], validated["note"], actor.user_id)
        return {"ca_version": ca_version, "unbatched": self._unbatched_view(instrument=validated["instrument"])}

    def list_ca_versions(self, actor: Actor, instrument: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_ca_versions(instrument)

    # ---- 净额批次 ----

    def generate_batch(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        """按账户、币种、交收日生成净额批次；同一批次号重提幂等并继续核对。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_generate_batch(actor.role):
            raise PermissionDenied("角色无权生成净额批次")
        req = self.rules.validate_batch_request(data)
        existing = self.repository.get_batch_by_no(req["batch_no"])
        if existing is not None:
            return self._replay_batch(existing, req)
        candidates = self._unbatched_view(account=req["account"], currency=req["currency"], settlement_day=req["settlement_day"])
        if not candidates:
            raise ValidationError("该账户/币种/交收日下没有可入批的指令")
        totals, net_cash = net_totals(candidates)
        try:
            batch = self.repository.create_batch(req["batch_no"], req["account"], req["currency"], req["settlement_day"], candidates, totals, net_cash, actor.user_id)
        except BatchNoExists:
            return self._replay_batch(self.repository.get_batch_by_no(req["batch_no"]), req)
        return self._batch_view(batch, replayed=False)

    def get_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._batch_view(self.repository.get_batch(batch_id))

    def list_batches(self, actor: Actor, state: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_batches(state=state)

    def confirm_batch(self, actor: Actor, batch_id: int, expected_version: int) -> Dict[str, Any]:
        """主管确认：冻结每笔指令采用的公司行动版本与净收付数量；并发确认仅一人成功。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_confirm_batch(actor.role):
            raise PermissionDenied("角色无权确认批次")
        self.repository.get_batch(batch_id)
        try:
            confirmed = self.repository.confirm_batch(batch_id, int(expected_version), actor.user_id)
        except Conflict as exc:
            latest = self.repository.get_batch(batch_id)
            exc.details = {
                "batch_no": latest["batch_no"],
                "batch_state": latest["state"],
                "batch_version": latest["version"],
                "unbatched": self._unbatched_view(account=latest["account"], currency=latest["currency"], settlement_day=latest["settlement_day"]),
            }
            raise
        return self._batch_view(confirmed)

    def cancel_batch(self, actor: Actor, batch_id: int, expected_version: int, reason: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_confirm_batch(actor.role):
            raise PermissionDenied("角色无权取消批次")
        reason = text({"reason": reason}, "reason")
        cancelled = self.repository.cancel_batch(batch_id, int(expected_version), actor.user_id, reason)
        return self._batch_view(cancelled)

    def unbatched_instructions(self, actor: Actor, account: Optional[str] = None, currency: Optional[str] = None, settlement_day: Optional[int] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._unbatched_view(account=account, currency=currency, settlement_day=settlement_day)

    # ---- 旧数据升级 ----

    def migrate_legacy(self) -> Dict[str, Any]:
        """旧数据升级：按指令原始公司行动参数为每个标的补登版本1，可重复执行。"""
        return {"backfilled": self.repository.backfill_legacy_ca_versions()}

    # ---- 内部 ----

    def _unbatched_view(self, instrument: Optional[str] = None, account: Optional[str] = None, currency: Optional[str] = None, settlement_day: Optional[int] = None) -> List[Dict[str, Any]]:
        """未入批指令按最新公司行动版本重算；已入批指令以批次冻结为准，不在此重算。"""
        locked = self.repository.locked_record_ids()
        view: List[Dict[str, Any]] = []
        for record in self.repository.list_records(limit=500):
            if record["id"] in locked or record["state"] not in BATCHABLE_STATES:
                continue
            payload = record["payload"]
            if instrument is not None and payload.get("instrument") != instrument:
                continue
            if account is not None and payload.get("account", "DEFAULT") != account:
                continue
            if currency is not None and payload.get("currency") != currency:
                continue
            if settlement_day is not None and int(payload.get("settlement_day", -1)) != int(settlement_day):
                continue
            item = batch_item(payload, self.repository.latest_ca_version(payload["instrument"]))
            item.update({
                "record_id": record["id"],
                "reference": record["reference"],
                "state": record["state"],
                "account": payload.get("account", "DEFAULT"),
                "currency": payload["currency"],
                "settlement_day": payload["settlement_day"],
            })
            view.append(item)
        return view

    def _replay_batch(self, batch: Dict[str, Any], req: Dict[str, Any]) -> Dict[str, Any]:
        """同一批次号重提：键值一致则继续核对并返回原批次，不重复落库。"""
        if (batch["account"], batch["currency"], int(batch["settlement_day"])) != (req["account"], req["currency"], int(req["settlement_day"])):
            raise Conflict("批次号已被其他批次占用")
        return self._batch_view(batch, replayed=True)

    def _batch_view(self, batch: Dict[str, Any], replayed: bool = False) -> Dict[str, Any]:
        items = self.repository.batch_items(batch["id"])
        return {"batch": batch, "items": items, "replayed": replayed, "verification": self._verify_batch(batch, items)}

    def _verify_batch(self, batch: Dict[str, Any], items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """继续核对：明细齐全、合计一致、未确认批次的指令仍处于可入批状态。"""
        issues: List[str] = []
        for item in items:
            try:
                record = self.repository.get(item["record_id"])
            except NotFound:
                issues.append("指令%s不存在" % item["record_id"])
                continue
            if batch["state"] == "open" and record["state"] not in BATCHABLE_STATES:
                issues.append("指令%s状态已变为%s" % (record["id"], record["state"]))
        if len(items) != int(batch["item_count"]):
            issues.append("批次明细数量与批次头不符")
        totals, net_cash = net_totals(items)
        if totals != batch["totals"] or abs(net_cash - float(batch["net_cash"])) > 0.005:
            issues.append("批次合计与明细不符")
        return {"ok": not issues, "issues": issues, "checked_items": len(items)}
