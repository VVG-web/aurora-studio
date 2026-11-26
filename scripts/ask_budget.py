"""Time envelopes for Ask. Check verdicts and final status belong to P1."""
from dataclasses import dataclass

QA_MIN_RUN = 10.0
QA_GRACE = 5.0


def synthesis_reserve(total: float, step: float) -> float:
    return min(total, max(10.0, min(step, 0.03 * total)))


@dataclass(frozen=True)
class QABudget:
    want_s: float
    given_s: float
    outer_s: float
    deadline_left_s: float

    def evidence(self, used: float, role_floor: float) -> dict:
        return {"want_s": self.want_s, "given_s": self.given_s, "used_s": round(max(0.0, used), 3),
                "capped": self.given_s < self.want_s, "role_floor_s": role_floor,
                "deadline_left_s": self.deadline_left_s}


def qa_budget(want: float, answer_left: float, end_left: float, cap: float) -> QABudget:
    left = max(0.0, answer_left)
    inner = min(want, left)
    return QABudget(want, inner, min(cap, max(0.0, end_left), left, inner + QA_GRACE), left)


def qa_timed_out(result: dict) -> bool:
    error = result.get("error")
    return result.get("timed_out") is True or (
        isinstance(error, dict) and error.get("code") in ("deadline", "timeout"))
