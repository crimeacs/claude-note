"""Public company-workflow tasks and evaluator-only executable outcome oracle.

Provider inputs MUST come through public_view(). The runner must never send this
module, score_episode(), policy_configuration(), or evaluator source to a model.
This is a synthetic operational simulator, not an actual customer system.
"""
from __future__ import annotations
import copy
import hashlib
import json
import random

VERSION = 2
MAX_SEEDS = 5
MAX_ACTIONS = 2
ACTION_KEYS = frozenset(("request_id", "queue"))
PUBLIC_KEYS = frozenset(("id", "phase", "ordinal", "scope", "task",
                         "allowed_queues", "policy_revision", "announcement",
                         "ambient_observations"))
COMPANIES = ("Pinewick Studio", "Bellford Supply", "Cedarco Works",
             "Cobalt Workshop", "Driftline Services")
PHASES = ("acquire",)*3 + ("transfer",)*3 + ("correct",) + (
          "corrected_transfer",)*2 + ("interference",)*3
# Public tasks expose new procedures only through actual own action outcomes.
# A/B exposure order is counterbalanced 3/2 across five company configurations.
BASE_JOBS = (
    ("share", "restricted", "sales", "north"),
    ("share", "public", "support", "west"),
    ("erase", "public", "operations", "east"),
)
A_EXPOSURE = ("export", "public", "legal", "north")
A_TRANSFER = ("export", "restricted", "legal", "west")
B_EXPOSURE = ("share", "public", "operations", "external")
B_TRANSFER = ("export", "public", "research", "external")

def _jobs(seed):
    first, first_transfer, second, second_transfer = (
        (A_EXPOSURE, A_TRANSFER, B_EXPOSURE, B_TRANSFER)
        if seed % 2 == 0 else
        (B_EXPOSURE, B_TRANSFER, A_EXPOSURE, A_TRANSFER))
    return BASE_JOBS + (
        first, first_transfer, second,
        ("share", "restricted", "support", "east"),
        second_transfer,
        ("export", "restricted", "operations", "south"),
        ("share", "restricted", "finance", "external"),
        ("export", "restricted", "legal", "external"),
        ("export", "public", "legal", "external"),
    )

def curriculum(seed):
    """Evaluator-only phase interpretation, not a provider-supplied lesson."""
    _seed(seed)
    return {
        "order": "A-first" if seed % 2 == 0 else "B-first",
        "procedure_A": "legal-team export exception",
        "procedure_B": "external-region transfer exception",
        "exposure_episodes": [1, 2, 3, 4, 6, 7, 11],
        "primary_transfer_episodes": [5, 8, 9, 10, 12],
        "early_transfer_episodes": [5],
        "middle_transfer_episodes": [8, 9],
        "late_transfer_episodes": [10, 12],
    }

def _seed(seed):
    if type(seed) is not int or not 0 <= seed < MAX_SEEDS:
        raise ValueError("seed must be an exact integer from 0 through 4")
    return seed

def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

def policy_configuration(seed):
    """Evaluator-only truth. Four opaque labels avoid prior-world queue guessing."""
    seed = _seed(seed)
    rng = random.Random(879134 + seed * 104729)
    numbers = rng.sample(range(11, 98), 4)
    labels = ["Q-" + str(value) for value in numbers]
    # Do not expose this role mapping through public_view or corpus.json.
    return {"general": labels[0], "restricted_r1": labels[1],
            "erase": labels[2], "restricted_r2": labels[3]}

def _ambient(seed, ordinal):
    if ordinal < 10:
        return []
    configuration = policy_configuration(seed)
    public_queues = sorted(configuration.values())
    scope = {"company_id": "company-%02d" % seed, "workflow": "facilities"}
    events = [
        {"observation_id": "facilities-%02d-1" % seed, "scope": scope,
         "text": "Facilities work-order dispatch uses %s for the loading dock."
                 % public_queues[0]},
        {"observation_id": "facilities-%02d-2" % seed, "scope": scope,
         "text": "Facilities dispatch sends key replacement to %s; customer-records routing is a separate workflow."
                 % public_queues[1]},
        {"observation_id": "facilities-%02d-3" % seed, "scope": scope,
         "text": "Facilities work order F-%02d closed after a desk move; no customer record was routed." % seed},
    ]
    # Bounded repeated deliveries, shared identically with every arm.
    indexes = {10: (0, 1, 0), 11: (1, 2, 1), 12: (2, 0, 2)}[ordinal]
    return [copy.deepcopy(events[i]) for i in indexes]

def trajectory(seed):
    """Twelve public tasks, containing no expected actions or generalized lessons."""
    seed = _seed(seed)
    configuration = policy_configuration(seed)
    # Stable public order is deliberately independent of the hidden role mapping.
    allowed = sorted(configuration.values())
    episodes = []
    for ordinal, (operation, visibility, team, region) in enumerate(_jobs(seed), 1):
        episode_id = "lc-s%02d-e%02d" % (seed, ordinal)
        revision = "r1" if ordinal <= 6 else "r2"
        task = {
            "id": episode_id, "phase": PHASES[ordinal-1], "ordinal": ordinal,
            "scope": {"company_id": "company-%02d" % seed,
                      "workflow": "customer-records", "environment": "public-study"},
            "task": {
                "company": COMPANIES[seed],
                "goal": "Route this request to an accepted destination using the current company workflow.",
                "request": {"id": "req-s%02d-%02d" % (seed, ordinal),
                            "entity_id": "entity-s%02d-%02d" % (seed, ordinal),
                            "operation": operation, "visibility": visibility,
                            "team": team, "region": region},
            },
            "allowed_queues": allowed,
            "policy_revision": revision,
            "announcement": (
                "The customer-records routing configuration changed to revision r2 before this request."
                if ordinal == 7 else ""
            ),
            "ambient_observations": _ambient(seed, ordinal),
        }
        episodes.append(task)
    return episodes

def get_sequences(seed_count=MAX_SEEDS):
    if type(seed_count) is not int or not 1 <= seed_count <= MAX_SEEDS:
        raise ValueError("seed_count must be an exact integer from 1 through 5")
    return {"company-%02d" % seed: trajectory(seed) for seed in range(seed_count)}

def _resolve(task_id):
    if type(task_id) is not str:
        raise ValueError("unknown public task")
    for seed in range(MAX_SEEDS):
        for task in trajectory(seed):
            if task["id"] == task_id:
                return seed, task
    raise ValueError("unknown public task")

def public_view(task):
    """Validate exact known public task bytes; no unrecognized/evaluator fields pass."""
    if type(task) is not dict or set(task) != PUBLIC_KEYS:
        raise ValueError("unexpected public task schema")
    _, expected = _resolve(task.get("id"))
    if task != expected:
        raise ValueError("public task differs from frozen simulator input")
    return copy.deepcopy(expected)

def _matched_procedure(task):
    """Evaluator-only decision trace; no source or future tasks enter a prompt."""
    request = task["task"]["request"]
    if request["operation"] == "erase":
        return "erase"
    if request["team"] == "legal" and request["operation"] == "export":
        return "legal-export"
    if request["region"] == "external" and request["operation"] in ("share", "export"):
        return "external-transfer"
    if request["visibility"] == "restricted":
        return "restricted-base"
    return "public-base"

def _expected(seed, task):
    configuration = policy_configuration(seed)
    procedure = _matched_procedure(task)
    if procedure in ("erase", "legal-export"):
        return configuration["erase"]
    if procedure == "external-transfer":
        # This old destination remains valid after the base-route correction.
        return configuration["restricted_r1"]
    if procedure == "restricted-base":
        return configuration["restricted_r1" if task["policy_revision"] == "r1"
                             else "restricted_r2"]
    return configuration["general"]

def _current_trace(task):
    # Exact applied constraint for THIS executed item, not a future rule table.
    procedure = _matched_procedure(task)
    request = task["task"]["request"]
    fields = {
        "erase": ("operation",),
        "legal-export": ("team", "operation"),
        "external-transfer": ("region",),
        "restricted-base": ("visibility",),
        "public-base": ("visibility",),
    }[procedure]
    return {"constraint_id": procedure,
            "matched_current_inputs": {field: request[field] for field in fields}}

def evaluate_action(task_id, action):
    """Execute one synthetic operational action; return only its public feedback.

    A wrong but well-formed route exposes the destination required for THIS
    item, a legitimate operational correction. The applied routing trace names
    only the backend constraint and matched CURRENT inputs for this actual item.
    Learning includes backend explanations, not discovery from labels alone;
    no future-task answer, hidden role map or authored memory is supplied.
    """
    seed, task = _resolve(task_id)
    request_id = task["task"]["request"]["id"]
    feedback = {"request_id": request_id, "scope": copy.deepcopy(task["scope"]),
                "policy_revision": task["policy_revision"]}
    if type(action) is not dict or set(action) != ACTION_KEYS:
        feedback["observed_status"] = "not_executed"
        return {"success": False, "feedback": feedback,
                "reason_codes": ["invalid-action-schema"]}
    if type(action["request_id"]) is not str or action["request_id"] != request_id:
        feedback["observed_status"] = "not_executed"
        return {"success": False, "feedback": feedback,
                "reason_codes": ["request-id-mismatch"]}
    if type(action["queue"]) is not str or action["queue"] not in task["allowed_queues"]:
        feedback["observed_status"] = "not_executed"
        return {"success": False, "feedback": feedback,
                "reason_codes": ["queue-not-available"]}
    expected = _expected(seed, task)
    success = action["queue"] == expected
    feedback["attempted_queue"] = action["queue"]
    feedback["routing_trace"] = _current_trace(task)
    feedback["observed_status"] = "routed" if success else "route_rejected"
    if not success:
        feedback["required_queue"] = expected
    feedback["receipt_id"] = "receipt-" + hashlib.sha256(_encoded(
        {"task": task_id, "action": action, "success": success})).hexdigest()[:20]
    return {"success": success, "feedback": feedback,
            "reason_codes": [] if success else ["destination-mismatch"]}

def score_episode(task_id, actions):
    """Evaluator-only metrics. NEVER add these fields to provider messages.

    Missing attempts remain unavailable, not an invented wrong model response.
    One corrective retry is allowed, so final success alone has a known ceiling.
    """
    seed, task = _resolve(task_id)
    if type(actions) is not list or len(actions) > MAX_ACTIONS:
        raise ValueError("at most two actual action attempts")
    results = [evaluate_action(task_id, action) for action in actions]
    request = task["task"]["request"]
    config = policy_configuration(seed)
    stale = sum(
        task["policy_revision"] == "r2" and _matched_procedure(task) == "restricted-base"
        and type(action) is dict
        and action.get("queue") == config["restricted_r1"] for action in actions)
    overscoped = sum(
        task["policy_revision"] == "r2"
        and _matched_procedure(task) != "restricted-base"
        and type(action) is dict and action.get("queue") == config["restricted_r2"]
        for action in actions)
    invalid = sum(result["feedback"]["observed_status"] == "not_executed"
                  for result in results)
    return {
        "task_id": task_id, "phase": task["phase"],
        "curriculum_order": curriculum(seed)["order"],
        "primary_transfer": task["ordinal"] in curriculum(seed)["primary_transfer_episodes"],
        "available": bool(actions),
        "first_action_success": results[0]["success"] if results else None,
        "final_task_success": any(result["success"] for result in results) if results else None,
        "action_count": len(actions), "invalid_action_count": invalid,
        "valid_but_wrong_count": sum(not result["success"] for result in results)-invalid,
        "stale_route_count": stale, "overscoped_correction_count": overscoped,
        "successful_action_index": next((i+1 for i,result in enumerate(results)
                                         if result["success"]), None),
    }
