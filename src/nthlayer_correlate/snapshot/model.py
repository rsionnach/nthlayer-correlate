"""Model interface — the ZFC judgment boundary."""
from __future__ import annotations

import json
import structlog

from nthlayer_correlate.types import CorrelationGroup

logger = structlog.get_logger()


class ModelInterface:
    def __init__(self, model: str = "claude-sonnet-4-20250514", max_tokens: int = 4096):
        self._model = model
        self._max_tokens = max_tokens
        self._client = None  # lazy init

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    async def interpret(
        self,
        prompt: str,
        groups: list[CorrelationGroup],
        verdict_store=None,
    ) -> list:
        """Call model, parse response into verdicts.

        Returns list of Verdict objects (children + parent).
        If model call fails, returns template-based verdicts with confidence 0.0.
        """
        from nthlayer_learn import create as verdict_create

        try:
            response = await self._call_model(prompt)
            assessments = self._parse_response(response, groups)
        except Exception as e:
            logger.warning("model_call_failed", error=str(e))
            return self._create_template_verdicts(groups)

        # Create per-correlation verdicts
        child_verdicts = []
        for assessment in assessments:
            v = verdict_create(
                subject={
                    "type": "correlation",
                    "service": assessment.get("service"),
                    "ref": assessment.get("group_id", ""),
                    "summary": assessment.get("summary", ""),
                },
                judgment={
                    "action": assessment.get("action", "flag"),
                    "confidence": assessment.get("confidence", 0.5),
                    "reasoning": assessment.get("reasoning", ""),
                    "tags": assessment.get("tags", []),
                },
                producer={"system": "sitrep", "model": self._model},
            )
            child_verdicts.append(v)
            if verdict_store:
                verdict_store.put(v)

        # Create parent snapshot verdict
        overall_action = "escalate" if any(
            a.get("action") == "escalate" for a in assessments
        ) else "flag"
        overall_confidence = (
            sum(a.get("confidence", 0.5) for a in assessments) / len(assessments)
            if assessments else 0.0
        )

        # Collect state transition tags
        tags = []
        for a in assessments:
            tags.extend(a.get("tags", []))

        parent = verdict_create(
            subject={
                "type": "correlation",
                "service": None,
                "ref": "snapshot",
                "summary": f"Snapshot: {len(groups)} group(s), {len(assessments)} assessed",
            },
            judgment={
                "action": overall_action,
                "confidence": overall_confidence,
                "reasoning": f"Assessed {len(assessments)} correlation group(s)",
                "tags": list(set(tags)),
            },
            producer={"system": "sitrep", "model": self._model},
        )
        parent.lineage.children = [v.id for v in child_verdicts]
        if verdict_store:
            verdict_store.put(parent)

        return child_verdicts + [parent]

    async def _call_model(self, prompt: str) -> str:
        """Call the Anthropic API."""
        import asyncio
        client = self._get_client()

        response = await asyncio.to_thread(
            client.messages.create,
            model=self._model,
            max_tokens=self._max_tokens,
            system=self._build_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

    def _build_system_prompt(self) -> str:
        return """You are SitRep, a signal correlation agent. Analyze the correlation groups below and provide your assessment as JSON.

For each correlation group, assess:
1. Whether the signals are causally related (or just temporally coincidental)
2. Your confidence in the causal assessment (0.0-1.0)
3. Recommended action: "flag" (needs investigation), "defer" (watch and wait), or "escalate" (needs immediate attention)
4. Brief reasoning

If you recommend a state transition, include a tag like "state_transition:alert" or "state_transition:incident".

Respond with ONLY valid JSON in this format:
{
  "assessments": [
    {
      "group_id": "cg-xxx",
      "service": "primary-affected-service",
      "summary": "brief description",
      "action": "flag",
      "confidence": 0.74,
      "reasoning": "explanation",
      "tags": ["deploy", "latency"]
    }
  ]
}"""

    def _parse_response(self, response_text: str, groups: list[CorrelationGroup]) -> list[dict]:
        """Parse model JSON response."""
        # Strip markdown fences if present
        text = response_text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        data = json.loads(text)
        assessments = data.get("assessments", [])

        # Validate actions and clamp confidence before verdict creation
        valid_actions = {"flag", "escalate", "defer"}
        for a in assessments:
            if a.get("action") not in valid_actions:
                a["action"] = "flag"
            a["confidence"] = max(0.0, min(1.0, float(a.get("confidence", 0.5))))

        return assessments

    def _create_template_verdicts(self, groups: list[CorrelationGroup]) -> list:
        """Degraded mode: template-based verdicts with confidence 0.0."""
        from nthlayer_learn import create as verdict_create

        child_verdicts = []
        for group in groups:
            v = verdict_create(
                subject={
                    "type": "correlation",
                    "service": group.services[0] if group.services else None,
                    "ref": group.id,
                    "summary": group.summary,
                },
                judgment={
                    "action": "flag",
                    "confidence": 0.0,
                    "reasoning": "template-based, model unavailable",
                    "tags": ["degraded"],
                },
                producer={"system": "sitrep"},
            )
            child_verdicts.append(v)

        parent = verdict_create(
            subject={
                "type": "correlation",
                "service": None,
                "ref": "snapshot-degraded",
                "summary": f"Degraded snapshot: {len(groups)} group(s), no model assessment",
            },
            judgment={
                "action": "flag",
                "confidence": 0.0,
                "reasoning": "template-based, model unavailable",
                "tags": ["degraded"],
            },
            producer={"system": "sitrep"},
        )
        parent.lineage.children = [v.id for v in child_verdicts]

        return child_verdicts + [parent]
