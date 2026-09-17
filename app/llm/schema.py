from typing import Annotated, Literal

from pydantic import Field

from app.context.models import StrictModel, TaskType

Text = Annotated[str, Field(min_length=1, max_length=24000)]
Notes = Annotated[list[Text], Field(max_length=100)]


class QuestionAnswer(StrictModel):
    question_id: Text
    answer: Text


class ArtifactSpec(StrictModel):
    kind: Literal["DOCUMENT", "PRESENTATION", "SPREADSHEET", "CODE", "OTHER"]
    title: Text
    specification: Text


class Solution(StrictModel):
    summary: Text
    assignment_types: Annotated[list[TaskType], Field(min_length=1, max_length=10)]
    understanding: Text
    answer: Text
    question_answers: Annotated[list[QuestionAnswer], Field(max_length=200)]
    artifacts: Annotated[list[ArtifactSpec], Field(max_length=20)]
    assumptions: Notes
    uncertainties: Notes
    sources_used: Notes
    requires_user_input: bool
    warnings: Notes
