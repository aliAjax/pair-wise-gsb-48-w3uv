"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, choice, integer, optional_text, text
from .repository import Repository
from .rules import DomainRules


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
        # 先锁定创建时点的公司行动版本；比例已修正时直接拒绝，避免与旧版本指令混用
        with self.repository.transaction() as connection:
            if prepared["corporate_action"] != "none":
                latest = self.repository.get_latest_corporate(
                    prepared["instrument"], prepared["corporate_action"], connection
                )
                if latest is None:
                    latest = self.repository.ensure_first_corporate_version(
                        connection,
                        prepared["instrument"],
                        prepared["corporate_action"],
                        float(prepared["action_ratio"]),
                        actor.user_id,
                    )
                prepared = self.rules.bind_corporate_version(prepared, latest)
            else:
                prepared["action_version"] = 0
        # 版本确认后再做跨指令重复检查
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        with self.repository.transaction() as connection:
            record = self.repository.insert_record(
                connection, reference, self.rules.INITIAL_STATE, prepared, actor.user_id
            )
        return record

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

    # ---- 公司行动版本 -------------------------------------------------

    def publish_corporate(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_publish_corporate(actor.role):
            raise PermissionDenied("角色无权发布公司行动版本")
        parsed = self.rules.validate_corporate_revision(payload or {})
        return self.repository.publish_corporate(
            parsed["instrument"],
            parsed["corporate_action"],
            parsed["action_ratio"],
            parsed["remark"],
            actor.user_id,
        )

    def list_corporate_versions(self, actor: Actor, instrument: str = None, corporate_action: str = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_corporate_versions(instrument=instrument, corporate_action=corporate_action)

    # ---- 净额批次 -----------------------------------------------------

    @staticmethod
    def _validate_group(data: Dict[str, Any]) -> Dict[str, Any]:
        data = data or {}
        return {
            "batch_no": text(data, "batch_no"),
            "account": optional_text(data, "account", "DEFAULT") or "DEFAULT",
            "currency": choice(data, "currency", ["CNY", "USD", "HKD"]),
            "settlement_day": integer(data, "settlement_day", 0),
        }

    def generate_net_batch(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_batch(actor.role):
            raise PermissionDenied("角色无权生成净额批次")
        group = self._validate_group(payload)
        with self.repository.transaction() as connection:
            records = self.repository.eligible_approved_records(
                group["account"], group["currency"], group["settlement_day"], connection
            )
            if not records:
                raise ValidationError("该账户/币种/交收日下没有可入批的已复核指令")
            # 快照逐笔落库，创建后比例再修正也不影响本批次，等待确认时冻结
            snapshots = [self.rules.net_snapshot(record) for record in records]
            totals = self.rules.batch_totals(snapshots)
            self.repository.create_net_batch(
                group["batch_no"],
                group["account"],
                group["currency"],
                group["settlement_day"],
                totals,
                snapshots,
                actor.user_id,
                connection=connection,
            )
        return self.repository.get_batch_detail(group["batch_no"])

    def get_batch(self, actor: Actor, batch_no: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_batch_detail(text({"batch_no": batch_no}, "batch_no"))

    def list_batches(self, actor: Actor, status: str = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_batches(status=status, limit=limit)

    def _leftover_preview(self, connection: Any, batch: Dict[str, Any], exclude_current_batch: bool = True) -> List[Dict[str, Any]]:
        """未入批指令预览：存在新版本时按新版本计算，但不落库（用于冲突响应，随事务回滚）。"""
        records = self.repository.eligible_approved_records(
            batch["account"],
            batch["currency"],
            batch["settlement_day"],
            connection,
            exclude_batch_id=int(batch["id"]) if exclude_current_batch else None,
        )
        leftovers = []
        for record in records:
            payload = record["payload"]
            if payload.get("corporate_action", "none") != "none":
                latest = self.repository.get_latest_corporate(
                    payload["instrument"], payload["corporate_action"], connection
                )
                if latest is not None and int(latest["version"]) != int(payload.get("action_version", 0) or 0):
                    payload = self.rules.recompute_payload(payload, latest)
            leftovers.append(self.rules.net_snapshot({"id": record["id"], "reference": record["reference"], "version": record["version"], "payload": payload}))
        return leftovers

    @staticmethod
    def _frozen_payload(payload: Dict[str, Any], item: Dict[str, Any], batch: Dict[str, Any]) -> Dict[str, Any]:
        frozen = dict(payload)
        # 冻结本笔结算指令采用的公司行动版本与净收付数量
        frozen["corporate_applied"] = True
        frozen["effective_quantity"] = int(item["settlement_quantity"])
        frozen["frozen_action_version"] = int(item["action_version"])
        frozen["frozen_action_ratio"] = float(item["action_ratio"])
        frozen["frozen_quantity"] = int(item["signed_quantity"])
        frozen["frozen_net_cash_amount"] = round(float(item["net_cash_amount"]), 2)
        frozen["net_batch_no"] = batch["batch_no"]
        return frozen

    def confirm_net_batch(self, actor: Actor, batch_no: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_batch(actor.role):
            raise PermissionDenied("角色无权确认净额批次")
        batch_no = text({"batch_no": batch_no}, "batch_no")
        replayed = False
        leftovers: List[Dict[str, Any]] = []

        with self.repository.transaction() as connection:
            batch = self.repository.require_batch(batch_no, connection)
            items = self.repository.list_batch_items(int(batch["id"]), connection)

            # 幂等重提：已确认批次直接返回，继续核对也不会产生副作用
            if batch["status"] == "confirmed":
                replayed = True
                leftovers = self._leftover_preview(connection, batch)
            else:
                records_by_id: Dict[int, Dict[str, Any]] = {}
                collisions: List[Dict[str, Any]] = []
                stale: List[Dict[str, Any]] = []
                for item in items:
                    record = self.repository.get_record_for_update(connection, int(item["record_id"]))
                    records_by_id[int(item["record_id"])] = record
                    other = self.repository.frozen_item_of_other_batch(connection, int(item["record_id"]), int(batch["id"]))
                    if other is not None:
                        # 冲突优先：该指令已被胜出批次冻结，不再重复标记为过期
                        collisions.append(
                            {
                                "record_id": int(item["record_id"]),
                                "reference": record["reference"],
                                "winning_batch_no": other["batch_no"],
                            }
                        )
                    elif int(record["version"]) != int(item["record_version"]) or record["state"] != "approved":
                        stale.append(
                            {
                                "record_id": int(item["record_id"]),
                                "reference": record["reference"],
                                "state": record["state"],
                                "current_version": int(record["version"]),
                                "batched_version": int(item["record_version"]),
                            }
                        )

                if collisions or stale:
                    # 败方视角的未入批指令：该分组下仍可重新组批的已复核、未冻结指令（含被赢家重算过新版本的指令）
                    leftovers = self._leftover_preview(connection, batch, exclude_current_batch=False)
                    raise Conflict(
                        "批次与其他确认冲突或指令已变更，请按未入批指令重新生成批次",
                        {"collisions": collisions, "stale": stale, "leftovers": leftovers},
                    )

                # 未入批指令先按最新公司行动版本重算并落库
                leftovers = []
                leftover_records = self.repository.eligible_approved_records(
                    batch["account"], batch["currency"], batch["settlement_day"], connection,
                    exclude_batch_id=int(batch["id"]),
                )
                for record in leftover_records:
                    payload = record["payload"]
                    new_payload = payload
                    if payload.get("corporate_action", "none") != "none":
                        latest = self.repository.get_latest_corporate(
                            payload["instrument"], payload["corporate_action"], connection
                        )
                        if latest is not None and int(latest["version"]) != int(payload.get("action_version", 0) or 0):
                            new_payload = self.rules.recompute_payload(payload, latest)
                            record = self.repository.update_record_tx(
                                connection,
                                int(record["id"]),
                                int(record["version"]),
                                record["state"],
                                new_payload,
                                actor.user_id,
                                "corporate_recomputed",
                                {"new_version": int(latest["version"]), "new_ratio": latest["action_ratio"], "reason": "批次确认时未入批指令按新版本重算"},
                            )
                            self.repository.add_batch_audit(
                                connection, int(batch["id"]), actor.user_id, "leftover_recomputed",
                                {"record_id": int(record["id"]), "reference": record["reference"], "action_version": int(latest["version"])},
                            )
                    leftovers.append(self.rules.net_snapshot(record))

                # 逐笔冻结版本与净收付数量并交收
                frozen_snapshots = []
                for item in items:
                    record = records_by_id[int(item["record_id"])]
                    frozen_payload = self._frozen_payload(record["payload"], item, batch)
                    self.repository.update_record_tx(
                        connection,
                        int(record["id"]),
                        int(record["version"]),
                        "settled",
                        frozen_payload,
                        actor.user_id,
                        "batch_settled",
                        {
                            "summary": "净额批次确认，冻结公司行动版本与净收付数量",
                            "batch_no": batch_no,
                            "action_version": int(item["action_version"]),
                            "signed_quantity": int(item["signed_quantity"]),
                            "net_cash_amount": round(float(item["net_cash_amount"]), 2),
                        },
                    )
                    self.repository.mark_item_frozen(connection, int(item["id"]))
                    frozen_snapshots.append(
                        {
                            "record_id": int(item["record_id"]),
                            "reference": record["reference"],
                            "side": record["payload"]["side"],
                            "record_version": int(item["record_version"]),
                            "action_version": int(item["action_version"]),
                            "action_ratio": float(item["action_ratio"]),
                            "settlement_quantity": int(item["settlement_quantity"]),
                            "signed_quantity": int(item["signed_quantity"]),
                            "net_cash_amount": round(float(item["net_cash_amount"]), 2),
                        }
                    )

                totals = self.rules.batch_totals(frozen_snapshots)
                self.repository.confirm_batch_row(connection, int(batch["id"]), totals, actor.user_id)
                self.repository.add_batch_audit(
                    connection,
                    int(batch["id"]),
                    actor.user_id,
                    "batch_confirmed",
                    {"totals": totals, "frozen": len(frozen_snapshots), "leftovers": len(leftovers)},
                )

        # 事务提交后读取最终结果；失败重提时仍能拿到同一批次
        result = self.repository.get_batch_detail(batch_no)
        result["replayed"] = replayed
        if not replayed:
            result["leftovers"] = leftovers
        else:
            result["leftovers"] = []
        return result
