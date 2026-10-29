"""Small evidence-backed emotion vector; independent arousal is not physical energy."""

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

EMOTIONS={'happy':'开心','sad':'低落','angry':'生气','curious':'好奇','awkward':'尴尬',
          'excited':'兴奋','surprised':'惊讶','proud':'得意','worried':'担忧'}
Fraction=Annotated[float,Field(ge=0,le=1,strict=True,allow_inf_nan=False)]


class AffectProfile(BaseModel):
    model_config=ConfigDict(extra='forbid')
    emotions: dict[str,Fraction]=Field(default_factory=dict,max_length=len(EMOTIONS))
    arousal: Fraction | None=None

    @field_validator('emotions')
    @classmethod
    def known_emotions(cls,value):
        if set(value)-EMOTIONS.keys():raise ValueError('unknown emotion category')
        return {key:score for key,score in value.items() if score>0}

    def scaled(self,factor):
        return AffectProfile(emotions={key:value*factor for key,value in self.emotions.items()},
            arousal=None if self.arousal is None else self.arousal*factor)

    def within(self,intensity):
        if max(self.emotions.values(),default=0)>intensity+1e-12:
            raise ValueError('emotion component exceeds aggregate feeling intensity')
        return self


EmotionScores=Annotated[dict[str,float],BeforeValidator(lambda value:AffectProfile(emotions=value).emotions)]
