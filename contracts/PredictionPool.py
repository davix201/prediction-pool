# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from dataclasses import dataclass
import json
import re

ERROR_EXPECTED = "[EXPECTED]"
ERROR_EXTERNAL = "[EXTERNAL]"
ERROR_TRANSIENT = "[TRANSIENT]"
ERROR_LLM = "[LLM_ERROR]"

SOURCE_SNIPPET_LIMIT = 6000


def _strip_trailing_commas(text: str) -> str:
    return re.sub(r",(?!\s*?[\{\[\"\'\w])", "", text)


def _parse_llm_json(raw) -> dict:
    """Defensively parse LLM output that should contain a JSON object."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise gl.vm.UserError(
            f"{ERROR_LLM} expected JSON object, got {type(raw).__name__}"
        )
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise gl.vm.UserError(f"{ERROR_LLM} no JSON object found in model output")
    fragment = _strip_trailing_commas(raw[start : end + 1])
    try:
        parsed = json.loads(fragment)
    except ValueError:
        raise gl.vm.UserError(f"{ERROR_LLM} malformed JSON in model output")
    if not isinstance(parsed, dict):
        raise gl.vm.UserError(f"{ERROR_LLM} JSON payload is not an object")
    return parsed


def _field(payload: dict, aliases):
    for name in aliases:
        if name in payload and payload[name] is not None:
            return payload[name]
    return None


def _coerce_int(value) -> int:
    if isinstance(value, bool):
        raise gl.vm.UserError(f"{ERROR_LLM} boolean where number expected")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        negative = text.startswith("-")
        digits = re.sub(r"[^0-9]", "", text)
        if digits == "":
            raise gl.vm.UserError(f"{ERROR_LLM} cannot coerce value to int")
        number = int(digits)
        return -number if negative else number
    raise gl.vm.UserError(f"{ERROR_LLM} cannot coerce value to int")


def _fetch_source(source_url: str) -> str:
    """GET the source URL with status classification (shared leader/validator)."""
    res = gl.nondet.web.get(str(source_url))
    status = int(getattr(res, "status", 0) or 0)
    if status < 200 or status > 299:
        if status >= 500:
            raise gl.vm.UserError(f"{ERROR_TRANSIENT} source returned HTTP {status}")
        raise gl.vm.UserError(f"{ERROR_EXTERNAL} source returned HTTP {status}")
    body = getattr(res, "body", None)
    if body is None:
        raise gl.vm.UserError(f"{ERROR_EXTERNAL} source returned empty body")
    try:
        return res.body.decode("utf-8")[:SOURCE_SNIPPET_LIMIT]
    except Exception:
        raise gl.vm.UserError(f"{ERROR_EXTERNAL} source body is not valid utf-8")


def _judge_pool(question: str, options, source_url: str):
    """Fetch the single source and ask which option won.

    Returns (winner_index, reason). winner_index == -1 means the source does
    not determine a winner and the pool should be voided.
    """
    snippet = _fetch_source(source_url)
    options_block = "\n".join(
        "index " + str(i) + ": " + str(o) for i, o in enumerate(options)
    )
    prompt = (
        "You are resolving a prediction market from one authoritative source.\n"
        "QUESTION:\n" + question + "\n\nOPTIONS:\n" + options_block + "\n\n"
        "SOURCE CONTENT:\n" + snippet + "\n\n"
        'Reply with ONLY a JSON object: {"winner_index": <integer index of the winning '
        'option, or -1 if the source does not determine a winner>, '
        '"reason": "<short explanation>"}\n'
        "The winner_index must be one of the listed indexes (or -1 for indeterminate)."
    )
    out = gl.nondet.exec_prompt(prompt, response_format="json")
    payload = _parse_llm_json(out)
    raw_index = _field(
        payload,
        ("winner_index", "winning_index", "winner", "index", "outcome_index"),
    )
    if raw_index is None:
        raise gl.vm.UserError(f"{ERROR_LLM} missing winner_index field")
    winner_index = _coerce_int(raw_index)
    if winner_index < -1 or winner_index >= len(options):
        raise gl.vm.UserError(f"{ERROR_LLM} winner_index out of range")
    reason = str(_field(payload, ("reason", "explanation", "rationale")) or "")
    return winner_index, reason


def _handle_leader_error(leaders_res, leader_fn):
    """Decide whether a failed leader run matches our own rerun failure."""
    leader_msg = leaders_res.message if hasattr(leaders_res, "message") else ""
    try:
        leader_fn()
        return False
    except gl.vm.UserError as e:
        vm_msg = e.message if hasattr(e, "message") else str(e)
        if vm_msg.startswith(ERROR_EXPECTED) or vm_msg.startswith(ERROR_EXTERNAL):
            return vm_msg == leader_msg
        if vm_msg.startswith(ERROR_TRANSIENT) and leader_msg.startswith(ERROR_TRANSIENT):
            return True
        return False
    except Exception:
        return False


class PredictionPool(gl.Contract):
    owner: Address
    pool_exists: TreeMap[str, bool]
    question: TreeMap[str, str]
    options_json: TreeMap[str, str]
    source_url: TreeMap[str, str]
    closes_at_iso: TreeMap[str, str]
    pool_resolved: TreeMap[str, bool]
    result_idx_json: TreeMap[str, str]
    result_reason: TreeMap[str, str]
    bet_choices: TreeMap[str, TreeMap[Address, i256]]
    bet_amounts: TreeMap[str, TreeMap[Address, u256]]
    bet_order: TreeMap[str, DynArray[Address]]

    def __init__(self):
        self.owner = gl.message.sender_address

    @gl.public.write
    def create_pool(
        self,
        pool_id: str,
        question: str,
        options: DynArray[str],
        source_url: str,
        closes_at_iso: str,
    ) -> None:
        if gl.message.sender_address != self.owner:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} only owner can create pools")
        if pool_id == "" or question == "" or source_url == "" or closes_at_iso == "":
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} pool_id, question, source_url and closes_at_iso are required"
            )
        if self.pool_exists.get(pool_id, False):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} duplicate pool id")
        labels = [str(o) for o in options]
        if len(labels) < 2 or len(labels) > 4:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} pool needs between 2 and 4 options")
        if not str(source_url).lower().startswith(("http://", "https://")):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} source_url must be http(s)")
        self.pool_exists[pool_id] = True
        self.question[pool_id] = question
        self.options_json[pool_id] = json.dumps(labels)
        self.source_url[pool_id] = str(source_url)
        self.closes_at_iso[pool_id] = str(closes_at_iso)
        self.pool_resolved[pool_id] = False
        self.result_idx_json[pool_id] = ""
        self.result_reason[pool_id] = ""

    @gl.public.write
    def bet(self, pool_id: str, option_idx: i256, amount_atto: u256, now_iso: str) -> None:
        if not self.pool_exists.get(pool_id, False):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unknown pool id")
        if self.pool_resolved.get(pool_id, False):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} pool already resolved")
        closes_at = self.closes_at_iso.get(pool_id, "")
        if str(now_iso) >= closes_at:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} betting is closed for this pool")
        idx = int(option_idx)
        options = self._options_of(pool_id)
        if idx < 0 or idx >= len(options):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} option index out of range")
        if int(amount_atto) <= 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} amount_atto must be positive")
        better = gl.message.sender_address
        choices = self.bet_choices.get_or_insert_default(pool_id)
        if better in choices:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} address already bet on this pool")
        choices[better] = i256(idx)
        amounts = self.bet_amounts.get_or_insert_default(pool_id)
        amounts[better] = u256(amount_atto)
        order = self.bet_order.get_or_insert_default(pool_id)
        order.append(better)

    @gl.public.write
    def resolve(self, pool_id: str, now_iso: str) -> bool:
        if not self.pool_exists.get(pool_id, False):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unknown pool id")
        if self.pool_resolved.get(pool_id, False):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} pool already resolved")
        closes_at = self.closes_at_iso.get(pool_id, "")
        if str(now_iso) < closes_at:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} pool is still open")
        question = self.question.get(pool_id, "")
        options = self._options_of(pool_id)
        source_url = self.source_url.get(pool_id, "")

        def _leader_fn() -> str:
            winner_index, reason = _judge_pool(question, options, source_url)
            return json.dumps({"winner_index": winner_index, "reason": reason})

        def _validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return _handle_leader_error(leaders_res, _leader_fn)
            try:
                leader_payload = _parse_llm_json(leaders_res.calldata)
                leader_raw = _field(
                    leader_payload,
                    ("winner_index", "winning_index", "winner", "index"),
                )
                if leader_raw is None:
                    return False
                leader_index = _coerce_int(leader_raw)
            except gl.vm.UserError:
                return False
            except Exception:
                return False
            my_index, _my_reason = _judge_pool(question, options, source_url)
            return my_index == leader_index

        agreed = gl.vm.run_nondet_unsafe(_leader_fn, _validator_fn)
        payload = _parse_llm_json(str(agreed))
        raw_index = _field(
            payload,
            ("winner_index", "winning_index", "winner", "index", "outcome_index"),
        )
        if raw_index is None:
            raise gl.vm.UserError(f"{ERROR_LLM} consensus output missing winner_index")
        winner_index = _coerce_int(raw_index)
        reason = str(_field(payload, ("reason", "explanation", "rationale")) or "")
        self.pool_resolved[pool_id] = True
        self.result_reason[pool_id] = reason
        if winner_index < 0:
            self.result_idx_json[pool_id] = "void"
        else:
            self.result_idx_json[pool_id] = str(winner_index)
        return True

    def _options_of(self, pool_id: str):
        try:
            options = json.loads(self.options_json.get(pool_id, "[]"))
        except ValueError:
            options = []
        if not isinstance(options, list):
            options = []
        return options

    @gl.public.view
    def get_owner(self) -> str:
        return str(self.owner)

    @gl.public.view
    def get_pool(self, pool_id: str) -> dict:
        if not self.pool_exists.get(pool_id, False):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unknown pool id")
        resolved = bool(self.pool_resolved.get(pool_id, False))
        result_raw = self.result_idx_json.get(pool_id, "")
        if not resolved:
            result = ""
        elif result_raw == "void":
            result = "void"
        else:
            result = int(result_raw)
        return {
            "pool_id": pool_id,
            "question": self.question.get(pool_id, ""),
            "options": self._options_of(pool_id),
            "source_url": self.source_url.get(pool_id, ""),
            "closes_at_iso": self.closes_at_iso.get(pool_id, ""),
            "resolved": resolved,
            "result": result,
            "reason": self.result_reason.get(pool_id, ""),
        }

    @gl.public.view
    def get_bet(self, pool_id: str, bettor: Address) -> dict:
        if not self.pool_exists.get(pool_id, False):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unknown pool id")
        key = bettor if isinstance(bettor, Address) else Address(bytes(bettor))
        choices = self.bet_choices.get(pool_id)
        amounts = self.bet_amounts.get(pool_id)
        choice = -1 if choices is None else int(choices.get(key, i256(-1)))
        amount = u256(0) if amounts is None else u256(amounts.get(key, u256(0)))
        return {"option_idx": choice, "amount_atto": amount}

    @gl.public.view
    def pot_total(self, pool_id: str) -> u256:
        if not self.pool_exists.get(pool_id, False):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unknown pool id")
        total = 0
        amounts = self.bet_amounts.get(pool_id)
        if amounts is not None:
            for _addr, amount in amounts.items():
                total += int(amount)
        return u256(total)

    @gl.public.view
    def winner(self, pool_id: str) -> str:
        if not self.pool_exists.get(pool_id, False):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unknown pool id")
        if not self.pool_resolved.get(pool_id, False):
            return ""
        result_raw = self.result_idx_json.get(pool_id, "")
        if result_raw == "void":
            return "void"
        options = self._options_of(pool_id)
        idx = int(result_raw)
        if idx < 0 or idx >= len(options):
            return "void"
        return str(options[idx])
