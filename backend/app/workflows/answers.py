"""Common question lookup and evidence retained for reviewed choices."""

from app.outcomes import FailureKind, UseCaseError


def pending_question(questions, question_id, expired_message):
    question = next((item for item in questions if item["id"] == str(question_id)), None)
    if question is None:
        raise UseCaseError(FailureKind.CONFLICT, expired_message)
    return question


def recorded_choice(question, answer: str, operator: str, **details):
    return {
        "id": question["id"], "kind": question["kind"], "answer": answer,
        "evidence": question["evidence"], "operator": operator, **details,
    }
