from __future__ import annotations


def remove_prompt_echo(text: str, prompt: str) -> str:
    clean_text = text.strip()
    clean_prompt = prompt.strip()
    if not clean_text or not clean_prompt:
        return clean_text

    text_lines = clean_text.splitlines()
    prompt_lines = [line.strip() for line in clean_prompt.splitlines() if line.strip()]
    while text_lines and prompt_lines and text_lines[0].strip() == prompt_lines[0]:
        text_lines.pop(0)
        prompt_lines.pop(0)
    trimmed = "\n".join(text_lines).strip()

    if _norm_summary_text(trimmed) == _norm_summary_text(clean_prompt):
        return ""
    return trimmed


def _norm_summary_text(value: str) -> str:
    return " ".join(value.split()).strip().lower()
