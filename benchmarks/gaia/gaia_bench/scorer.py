"""GAIA scoring.

``question_scorer`` and its helpers are VENDORED VERBATIM from the official
GAIA leaderboard scorer. Do not "improve" them -- a scorer that is three
points generous invalidates every conclusion drawn from a run. The ``print``
calls are part of the original and are kept deliberately; they double as a
grading trace during a run.

Source: https://huggingface.co/spaces/gaia-benchmark/leaderboard (scorer.py)
Only change: dropped the original's unused ``json`` and ``numpy`` imports.

``extract_final_answer`` and ``lenient_scorer`` are ours. The gap between
strict and lenient scoring is the number of points recoverable from answer
formatting alone -- a prompt fix rather than a capability fix.
"""
from __future__ import annotations

import re
import string
import warnings

# --- vendored verbatim from the official GAIA scorer ------------------------


def normalize_number_str(number_str: str) -> float:
    # we replace these common units and commas to allow
    # conversion to float
    for char in ["$", "%", ","]:
        number_str = number_str.replace(char, "")
    try:
        return float(number_str)
    except ValueError:
        print(f"String {number_str} cannot be normalized to number str.")
        return float("inf")


def split_string(
    s: str,
    char_list: list[str] = [",", ";"],
) -> list[str]:
    pattern = f"[{''.join(char_list)}]"
    return re.split(pattern, s)


def question_scorer(
    model_answer: str,
    ground_truth: str,
) -> bool:
    def is_float(element: any) -> bool:
        try:
            float(element)
            return True
        except ValueError:
            return False

    if model_answer is None:
        model_answer = "None"

    # if gt is a number
    if is_float(ground_truth):
        print(f"Evaluating {model_answer} as a number.")
        normalized_answer = normalize_number_str(model_answer)
        return normalized_answer == float(ground_truth)

    # if gt is a list
    elif any(char in ground_truth for char in [",", ";"]):
        print(f"Evaluating {model_answer} as a comma separated list.")
        # question with the fish: normalization removes punct

        gt_elems = split_string(ground_truth)
        ma_elems = split_string(model_answer)

        # check length is the same
        if len(gt_elems) != len(ma_elems):
            warnings.warn(
                "Answer lists have different lengths, returning False.", UserWarning
            )
            return False

        # compare each element as float or str
        comparisons = []
        for ma_elem, gt_elem in zip(ma_elems, gt_elems):
            if is_float(gt_elem):
                normalized_ma_elem = normalize_number_str(ma_elem)
                comparisons.append(normalized_ma_elem == float(gt_elem))
            else:
                # we do not remove punct since comparisons can include punct
                comparisons.append(
                    normalize_str(ma_elem, remove_punct=False)
                    == normalize_str(gt_elem, remove_punct=False)
                )
        return all(comparisons)

    # if gt is a str
    else:
        print(f"Evaluating {model_answer} as a string.")
        return normalize_str(model_answer) == normalize_str(ground_truth)


def normalize_str(input_str, remove_punct=True) -> str:
    """
    Normalize a string by:
    - Removing all white spaces
    - Optionally removing punctuation (if remove_punct is True)
    - Converting to lowercase
    Parameters:
    - input_str: str, the string to normalize
    - remove_punct: bool, whether to remove punctuation (default: True)
    Returns:
    - str, the normalized string
    """
    # Remove all white spaces. Required e.g for seagull vs. sea gull
    no_spaces = re.sub(r"\s", "", input_str)

    # Remove punctuation, if specified.
    if remove_punct:
        translator = str.maketrans("", "", string.punctuation)
        return no_spaces.lower().translate(translator)
    else:
        return no_spaces.lower()


# --- end vendored -----------------------------------------------------------


_FINAL_ANSWER_RE = re.compile(r"FINAL\s+ANSWER\s*:\s*(.+)", re.IGNORECASE)


# Markdown emphasis the model wraps the marker or answer in. Models write
# "**FINAL ANSWER:** 8" often enough that not stripping these scores correct
# answers as wrong -- the regex matches inside the bold and captures "** 8".
_EMPHASIS_CHARS = "*_`"


def extract_final_answer(text: str) -> str | None:
    """Return the answer following the last FINAL ANSWER marker, or None."""
    if not text:
        return None
    matches = _FINAL_ANSWER_RE.findall(text)
    if not matches:
        return None
    answer = matches[-1].strip()
    # Strip emphasis then whitespace, repeatedly: "** 8" -> "8", and
    # "**a, b, c**" -> "a, b, c" without touching internal punctuation.
    prev = None
    while prev != answer:
        prev = answer
        answer = answer.strip(_EMPHASIS_CHARS).strip()
    return answer.rstrip(".").strip()


_ARTICLES = ("the ", "a ", "an ")
# Unit words GAIA answers commonly carry that the ground truth omits.
_TRAILING_UNITS = (
    "minutes", "minute", "hours", "hour", "seconds", "second",
    "days", "day", "years", "year", "percent", "usd", "dollars", "dollar",
    "kg", "km", "m", "cm", "mm", "lbs", "lb",
)


def _lenient_normalize(value: str) -> str:
    v = value.strip().lower()
    for article in _ARTICLES:
        if v.startswith(article):
            v = v[len(article):]
    for unit in _TRAILING_UNITS:
        if v.endswith(" " + unit):
            v = v[: -(len(unit) + 1)].strip()
    v = v.replace(",", "").replace("$", "").replace("%", "")
    v = v.translate(str.maketrans("", "", string.punctuation))
    return re.sub(r"\s+", "", v)


def _is_number(value: str) -> bool:
    try:
        float(value.strip().replace(",", "").replace("$", "").replace("%", ""))
    except ValueError:
        return False
    return True


def _tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def _contains_run(haystack: list[str], needle: list[str]) -> bool:
    """True if *needle* appears as a contiguous run inside *haystack*."""
    if not needle or len(needle) > len(haystack):
        return False
    return any(
        haystack[i : i + len(needle)] == needle
        for i in range(len(haystack) - len(needle) + 1)
    )


def lenient_scorer(model_answer: str, ground_truth: str) -> bool:
    """Grade ignoring formatting: articles, units, separators, punctuation.

    Never looser than that -- a wrong answer stays wrong. The delta against
    ``question_scorer`` is the formatting-only recoverable score.
    """
    if question_scorer(model_answer, ground_truth):
        return True
    if _lenient_normalize(model_answer) == _lenient_normalize(ground_truth):
        return True

    # "Correct content, wrong shape": the right answer padded with words, or
    # phrased more tersely than the ground truth. Guards, each earned from a
    # real false positive on dev-001:
    #   - numbers: "6" is a substring of "16".
    #   - single tokens: one shared word proves nothing.
    #   - lists: the element count IS the answer. The official scorer fails a
    #     length mismatch, and containment matches a truncated or
    #     over-inclusive list trivially -- those are wrong, not mis-formatted.
    if any(ch in ground_truth for ch in (",", ";")):
        return False
    gt_tokens, ma_tokens = _tokens(ground_truth), _tokens(model_answer)
    if _is_number(ground_truth) or len(gt_tokens) < 2 or len(ma_tokens) < 2:
        return False
    return _contains_run(ma_tokens, gt_tokens) or _contains_run(gt_tokens, ma_tokens)
