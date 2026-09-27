"""Big-step (whole-theory) verification, split out of session.py to keep files modular.

`BigStepMixin` is mixed into `_Isabelle_Session` (server/app/services/session.py); its
methods reference `self.*` (step / save_state / restore_state / enter_thy / current_thy /
command_history / verified_theories / _result_output / _acquire_request / _release_request),
all defined on the main session class and resolved via the MRO at runtime.
"""
from __future__ import annotations

import re
import time
from typing import List, Optional, Tuple

from server.app.core.config import Timeouts
from server.app.core.logging import get_logger
from server.app.services.theory_parsing import extract_theory_name
from server.app.services.success_checker import get_error_message, is_syntax_successful

from .internal_models import BigStepExecuteResult

logger = get_logger(__name__)


class BigStepMixin:
    def _make_big_step_failure(
        self,
        *,
        stage: str,
        result=None,
        fallback_error: Optional[str] = None,
        execution_time: float,
        output_parts: Optional[List[str]] = None,
    ) -> BigStepExecuteResult:
        message = fallback_error
        if result is not None and fallback_error is None:
            try:
                message = get_error_message(result)
            except Exception:
                message = None
        if not message:
            message = f"Big-step verification failed during {stage}."
        else:
            message = f"Big-step verification failed during {stage}: {message}"

        output = "\n".join([part for part in (output_parts or []) if part]).strip() or None
        return BigStepExecuteResult(
            success=False,
            output=output,
            error=message,
            execution_time=execution_time,
        )

    def _split_complete_theory(self, theory_text: str) -> Tuple[str, str, str]:
        text = theory_text.strip()
        if not text:
            raise ValueError("Theory text is empty.")

        header_match = re.search(r"(?s)\btheory\b.*?\bbegin\b", text)
        if header_match is None:
            raise ValueError(
                "Big-step verification requires a complete theory file containing "
                "a theory header with imports and a 'begin' keyword."
            )

        end_match = re.search(r"\bend\s*$", text)
        if end_match is None:
            raise ValueError(
                "Big-step verification requires a complete theory file ending with a final 'end'."
            )

        if header_match.end() > end_match.start():
            raise ValueError("Theory body is malformed: final 'end' appears before the end of the header.")

        header = text[: header_match.end()].strip()
        body = text[header_match.end() : end_match.start()]
        end_keyword = text[end_match.start() : end_match.end()].strip()

        return header + "\n", body.strip(), end_keyword

    def _restore_big_step_state(
        self,
        checkpoint_id: Optional[int],
        previous_theory: str,
        timeout: float,
    ) -> Optional[str]:
        restore_errors: List[str] = []

        if checkpoint_id is not None:
            try:
                restored = self.restore_state(checkpoint_id, timeout=timeout)
                if restored is False:
                    restore_errors.append("restore_state returned false")
            except Exception as exc:
                restore_errors.append(f"restore_state error: {exc}")

        if previous_theory and previous_theory != self.entered_thy:
            try:
                self.enter_thy(previous_theory, timeout=timeout)
            except Exception as exc:
                restore_errors.append(f"failed to re-enter previous theory '{previous_theory}': {exc}")

        if restore_errors:
            logger.error("state restore encountered issues: %s", "; ".join(restore_errors))
            return "; ".join(restore_errors)
        logger.info("state restored successfully after big-step failure")
        return None

    def big_step(self, theory_name: str, proof: str, timeout: float = Timeouts.BIGSTEP_DEFAULT) -> BigStepExecuteResult:
        self.update_activity()
        self._acquire_request()
        start_time = time.time()
        checkpoint_id: Optional[int] = None
        output_parts: List[str] = []

        try:
            try:
                previous_theory = self.current_thy
            except Exception:
                previous_theory = ""

            declared_name = extract_theory_name(proof)
            if declared_name is None:
                execution_time = time.time() - start_time
                return self._make_big_step_failure(
                    stage="theory-header validation",
                    fallback_error="Theory text must start with a valid 'theory <Name>' declaration.",
                    execution_time=execution_time,
                )

            if declared_name != theory_name:
                execution_time = time.time() - start_time
                return self._make_big_step_failure(
                    stage="theory-name validation",
                    fallback_error=(
                        f"Declared theory name '{declared_name}' does not match request theory_name '{theory_name}'."
                    ),
                    execution_time=execution_time,
                )

            header_text, body_text, end_text = self._split_complete_theory(proof)
            checkpoint_id = self.save_state(timeout=timeout)
            self.enter_thy(theory_name, timeout=timeout)

            header_result = self.step(header_text, timeout=timeout)
            output_parts.append(self._result_output(header_result))
            if not is_syntax_successful(header_result):
                restore_note = self._restore_big_step_state(checkpoint_id, previous_theory, timeout)
                failure = self._make_big_step_failure(
                    stage="theory header/import processing",
                    result=header_result,
                    execution_time=time.time() - start_time,
                    output_parts=output_parts,
                )
                if restore_note:
                    failure.error = f"{failure.error} Restore note: {restore_note}"
                return failure

            if body_text.strip():
                body_result = self.step(body_text.strip() + "\n", timeout=timeout)
                output_parts.append(self._result_output(body_result))
                if not is_syntax_successful(body_result):
                    restore_note = self._restore_big_step_state(checkpoint_id, previous_theory, timeout)
                    failure = self._make_big_step_failure(
                        stage="theory body processing",
                        result=body_result,
                        execution_time=time.time() - start_time,
                        output_parts=output_parts,
                    )
                    if restore_note:
                        failure.error = f"{failure.error} Restore note: {restore_note}"
                    return failure

            end_result = self.step(end_text, timeout=timeout)
            output_parts.append(self._result_output(end_result))
            if not is_syntax_successful(end_result):
                restore_note = self._restore_big_step_state(checkpoint_id, previous_theory, timeout)
                failure = self._make_big_step_failure(
                    stage="final end verification",
                    result=end_result,
                    execution_time=time.time() - start_time,
                    output_parts=output_parts,
                )
                if restore_note:
                    failure.error = f"{failure.error} Restore note: {restore_note}"
                return failure

            execution_time = time.time() - start_time
            self.command_history.append(
                {
                    "command": f"[BIGSTEP] {theory_name}",
                    "timestamp": start_time,
                    "success": True,
                    "subgoals_count": 0,
                }
            )
            self.verified_theories.append(theory_name)
            logger.info(
                "big-step command finished theory_name=%s execution_time=%s",
                theory_name,
                round(execution_time, 3),
            )
            return BigStepExecuteResult(
                success=True,
                output="\n".join([part for part in output_parts if part]).strip() or None,
                error=None,
                execution_time=execution_time,
            )

        except Exception as exc:
            restore_note = self._restore_big_step_state(checkpoint_id, previous_theory, timeout)
            execution_time = time.time() - start_time
            failure = self._make_big_step_failure(
                stage="big-step execution",
                fallback_error=str(exc),
                execution_time=execution_time,
                output_parts=output_parts,
            )
            if restore_note:
                failure.error = f"{failure.error} Restore note: {restore_note}"
            return failure
        finally:
            self._release_request()
