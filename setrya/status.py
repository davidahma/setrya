"""Public result and process exit contract, shared by all Setrya drills."""
from enum import Enum


class Status(Enum):
    PASS = ("PASS", 0)
    FAIL = ("FAIL", 1)
    ERROR = ("ERROR", 2)
    NOT_TESTED = ("NOT TESTED", 3)

    @property
    def label(self) -> str:
        return self.value[0]

    @property
    def exit_code(self) -> int:
        return self.value[1]

    @classmethod
    def from_gate(cls, verdict: str) -> "Status":
        """Translate the frozen offboarding evidence vocabulary."""
        return {"PASS": cls.PASS, "FAIL": cls.FAIL, "INCONCLUSIVE": cls.NOT_TESTED}[verdict]


def exit_agrees(code, status, contract):
    if contract == "setrya":
        return code == status.exit_code
    return code in {0, 1} and (status is Status.NOT_TESTED or code == status.exit_code)
