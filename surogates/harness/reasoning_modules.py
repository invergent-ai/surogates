"""The 39 atomic reasoning modules from the SELF-DISCOVER paper.

Each module is a single-sentence cognitive heuristic.  The
``self_discover`` engine asks the auxiliary LLM to SELECT a small
subset that fits the task at hand, then produces a free-form JSON
reasoning structure that operationalizes them.  We deliberately keep
the names from the paper so the picked subset is meaningful when
read back from logs.

Source: Zhou et al. (2024), "SELF-DISCOVER: Large Language Models
Self-Compose Reasoning Structures." (arXiv:2402.03620)
"""

from __future__ import annotations


REASONING_MODULES: dict[str, str] = {
    "experimental_design": "How could I devise an experiment to help solve that problem?",
    "iterative_problem_solving": "Make a list of ideas for solving this problem and apply them one by one to see if any progress can be made.",
    "progress_measurement": "How could I measure progress on this problem?",
    "problem_simplification": "How can I simplify the problem so that it is easier to solve?",
    "assumption_analysis": "What are the key assumptions underlying this problem?",
    "risk_assessment": "What are the potential risks and drawbacks of each solution?",
    "perspective_analysis": "What are the alternative perspectives or viewpoints on this problem?",
    "long_term_implications": "What are the long-term implications of this problem and its solutions?",
    "problem_decomposition": "How can I break down this problem into smaller, more manageable parts?",
    "critical_thinking": "Analyze the problem from different perspectives, question assumptions, evaluate evidence, and identify logical flaws or biases in thinking.",
    "creative_thinking": "Generate innovative and out-of-the-box ideas. Explore unconventional solutions and think beyond traditional boundaries.",
    "collaborative_thinking": "Seek input from others; leverage diverse perspectives and expertise to find effective solutions.",
    "systems_thinking": "Consider the problem as part of a larger system. Identify underlying causes, feedback loops, and interdependencies; develop holistic solutions.",
    "risk_analysis": "Evaluate potential risks, uncertainties, and tradeoffs across solutions. Weigh consequences and likelihood, decide based on a balanced view.",
    "reflective_thinking": "Step back from the problem; examine personal biases, assumptions, and mental models that may be skewing your approach.",
    "core_issue_identification": "What is the core issue or problem that needs to be addressed?",
    "causal_analysis": "What are the underlying causes or factors contributing to the problem?",
    "historical_analysis": "Are there potential solutions or strategies that have been tried before? If yes, what were the outcomes and lessons learned?",
    "obstacle_identification": "What potential obstacles or challenges might arise in solving this problem?",
    "data_analysis": "Are there relevant data or information that can provide insights? What data sources are available, and how can they be analyzed?",
    "stakeholder_analysis": "Are there stakeholders or individuals directly affected by the problem? What are their perspectives and needs?",
    "resource_analysis": "What resources (financial, human, technological, etc.) are needed to tackle the problem effectively?",
    "success_metrics": "How can progress or success in solving the problem be measured or evaluated?",
    "metric_identification": "What indicators or metrics can be used?",
    "problem_type_technical": "Is the problem a technical or practical one that requires specific expertise, or is it more conceptual or theoretical?",
    "physical_constraints": "Does the problem involve a physical constraint, such as limited resources, infrastructure, or space?",
    "behavioral_aspects": "Is the problem related to human behavior -- a social, cultural, or psychological issue?",
    "decision_making": "Does the problem involve decision-making or planning, where choices must be made under uncertainty or with competing objectives?",
    "analytical_problem": "Is the problem an analytical one that requires data analysis, modeling, or optimization techniques?",
    "design_challenge": "Is the problem a design challenge that requires creative solutions and innovation?",
    "systemic_issues": "Does the problem require addressing systemic or structural issues rather than just individual instances?",
    "time_sensitivity": "Is the problem time-sensitive or urgent, requiring immediate attention and action?",
    "typical_solutions": "What kinds of solution are typically produced for this kind of problem specification?",
    "alternative_solutions": "Given the problem specification and a current best solution, guess at other possible solutions.",
    "radical_rethinking": "Assume the current best solution is totally wrong; what other ways are there to think about the problem?",
    "solution_modification": "What is the best way to modify the current best solution, given what you know about this kind of problem?",
    "novel_solution": "Ignoring the current best solution, create an entirely new solution to the problem.",
    "step_by_step": "Let's think step by step.",
    "step_by_step_plan": "Let's make a step-by-step plan and implement it with clear explanation.",
}


def render_module_library() -> str:
    """Return the modules as a `- name: description` block for prompt injection."""
    return "\n".join(f"- {name}: {desc}" for name, desc in REASONING_MODULES.items())
