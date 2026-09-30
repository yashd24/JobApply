r"""
Validates Claude's plan against the content bank. Anything that breaks a rule
is reverted to the original and reported — never silently accepted.

Rules for bullet edits ("only add JD keywords"):
  1. Every number in the original must survive unchanged (80,000+, 250+, 90%, ...).
  2. At most MAX_REMOVED original words may be dropped/replaced.
  3. At most MAX_ADDED new words may be added. Every added word must appear in
     the job description AND somewhere in your own content bank (so a JD term
     like "Kubernetes" can never be inserted unless you have written about it),
     or be a small connector like "and".
  4. LaTeX must stay safe: balanced braces, only \textbf / \% / \& allowed.
"""

from __future__ import annotations

import re
from collections import Counter

MAX_REMOVED = 4
MAX_ADDED = 8
CONNECTORS = {"and", "or", "with", "using", "via", "for", "the", "a", "an", "of",
              "to", "in", "on", "including", "such", "as", "by", "&", "based"}
ALLOWED_COMMANDS = {"textbf"}

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#./-]*")
_NUM = re.compile(r"\d[\d,.]*\+?%?")


def plain(latex: str) -> str:
    s = re.sub(r"\\textbf\{", "", latex)
    s = s.replace("\\%", "%").replace("\\&", "&").replace("---", " ").replace("--", "-")
    return s.replace("{", "").replace("}", "")


def words(latex: str) -> list[str]:
    return [w.lower().rstrip(".,;:") for w in _WORD.findall(plain(latex))]


def numbers(latex: str) -> Counter:
    return Counter(n.rstrip(".,") for n in _NUM.findall(plain(latex)))


def _structure_problem(original: str, edited: str) -> str | None:
    """LaTeX safety and the number-lock. Shared by both edit checkers."""
    if edited.count("{") != edited.count("}"):
        return "unbalanced braces"
    cmds = set(re.findall(r"\\([A-Za-z]+)", edited))
    bad = cmds - ALLOWED_COMMANDS
    if bad:
        return f"disallowed LaTeX commands: {sorted(bad)}"
    if re.search(r"(?<!\\)[%&#_$^~]", edited):
        return "unescaped LaTeX special character"
    if numbers(original) != numbers(edited):
        return f"numbers changed {dict(numbers(original))} -> {dict(numbers(edited))}"
    return None


# Grammar-only words that may be added even when the JD doesn't contain them
# (locked mode allows at most MAX_NON_JD_WORDS per bullet). Deliberately no
# nouns, verbs of work, or tool names.
LINKING_WORDS = CONNECTORS | {
    "also", "that", "which", "while", "is", "are", "be", "been", "was", "were", "it", "its",
    "their", "this", "these", "those", "each", "all", "both", "from", "into", "across", "over",
    "under", "between", "through", "within", "without", "when", "where", "than", "then", "so",
    "but", "not", "more", "most", "has", "have", "had", "will", "can", "per", "after", "before",
    "during", "both", "one", "two",
}
MAX_NON_JD_WORDS = 3
_TECH_SYMBOLS = re.compile(r"[/.+#]")

# Technologies that count as technologies whatever their capitalisation ("terraform", "grpc",
# "postgres" in a JD are tools even though they are lowercase). Single tokens as words() produces
# them. Deliberately left out because they are everyday English words: go, swift, express, flow, chef.
KNOWN_TECH = frozenset("""
python java javascript typescript kotlin scala golang rust ruby php perl csharp dotnet bash powershell
sql nosql plsql graphql grpc protobuf thrift soap openapi swagger
django flask fastapi tornado celery spring springboot hibernate rails laravel nodejs react angular vue
nextjs nuxt svelte jquery redux webpack
postgresql mysql mariadb sqlite oracle mssql mongodb dynamodb cassandra couchbase redis memcached
elasticsearch opensearch solr lucene neo4j influxdb timescaledb clickhouse snowflake bigquery redshift
kafka rabbitmq activemq zeromq nats pulsar sqs sns kinesis eventbridge celery airflow dagster luigi
spark pyspark hadoop hive hbase flink presto trino dbt databricks
aws gcp azure ec2 s3 rds alb elb ecs eks ecr lambda fargate cloudfront route53 iam vpc cloudformation
cloudwatch cloudtrail cdk googlecloud
docker kubernetes helm istio terraform terragrunt pulumi ansible puppet packer vagrant consul vault nomad
jenkins circleci travis gitlab github bitbucket argocd fluxcd teamcity bamboo
nginx apache haproxy envoy kong tomcat gunicorn uwsgi
linux unix ubuntu debian centos git svn mercurial jira confluence
prometheus grafana datadog splunk newrelic sentry jaeger opentelemetry kibana logstash elk nagios
oauth saml jwt keycloak auth0 okta ldap
selenium cypress playwright pytest junit testng mockito jest mocha
tensorflow pytorch keras sklearn pandas numpy scipy langchain openai huggingface
hubspot salesforce stripe twilio sendgrid zendesk intercom segment
postman insomnia figma
""".split())

# alias -> canonical name. Both sides of a backing check are mapped through this.
ALIASES = {
    "postgres": "postgresql", "psql": "postgresql", "postgre": "postgresql",
    "k8s": "kubernetes", "kube": "kubernetes", "gke": "kubernetes", "aks": "kubernetes",
    "mongo": "mongodb", "dynamo": "dynamodb", "elastic": "elasticsearch",
    "js": "javascript", "ts": "typescript", "node": "nodejs", "node.js": "nodejs", "golang": "go",
    "reactjs": "react", "react.js": "react", "vuejs": "vue", "vue.js": "vue", "next.js": "nextjs",
    "angularjs": "angular", "c#": "csharp", ".net": "dotnet",
    "rabbit": "rabbitmq", "amqp": "rabbitmq",
    "scikit-learn": "sklearn", "sk-learn": "sklearn", "torch": "pytorch",
    "gcloud": "gcp", "ms-sql": "mssql", "sqlserver": "mssql", "sql-server": "mssql",
    "spring-boot": "spring", "springboot": "spring",
}
# Ambiguous two-letter aliases (es, pg, tf, gh) are deliberately NOT aliases: too easy to misfire.


def canon(word: str) -> str:
    """Canonical technology name for a lowercase word (postgres -> postgresql, k8s -> kubernetes)."""
    w = word.lower()
    return ALIASES.get(w, w)


def is_known_tech(word: str) -> bool:
    w = word.lower()
    return w in KNOWN_TECH or w in ALIASES or canon(w) in KNOWN_TECH


def jd_tech_terms(jd_text: str) -> set[str]:
    """JD words that look like a technology/tool name: anything on the curated KNOWN_TECH list (any
    capitalisation), any word with a digit or one of / . + #, or a word capitalised in the middle
    of a sentence (not at the start of a line or sentence)."""
    terms: set[str] = set()
    for m in _WORD.finditer(jd_text):
        core = m.group().rstrip(".,;:")
        if not core:
            continue
        if is_known_tech(core):
            terms.add(core.lower())
        elif any(c.isdigit() for c in core) or _TECH_SYMBOLS.search(core):
            terms.add(core.lower())
        elif core[0].isupper():
            before = jd_text[:m.start()].rstrip(" \t")
            if before and before[-1] not in ".!?:\r\n":
                terms.add(core.lower())
    return terms


def check_edit_locked(original: str, edited: str, jd_words: set[str], tech_terms: set[str],
                      visible_words: set[str]) -> tuple[bool, str]:
    """Edit rules for locked mode.
    - LaTeX safety, the number-lock, MAX_REMOVED and MAX_ADDED are unchanged.
    - An added word must appear in the JD. If it looks like a technology (tech_terms) it
      must ALSO appear in the visible resume (visible_words). Ordinary descriptive JD
      words need no resume backing.
    - Up to MAX_NON_JD_WORDS added words may be absent from the JD, but only grammar
      linking words (LINKING_WORDS)."""
    problem = _structure_problem(original, edited)
    if problem:
        return False, problem

    old, new = Counter(words(original)), Counter(words(edited))
    removed, added = old - new, new - old
    if sum(removed.values()) > MAX_REMOVED:
        return False, f"too many original words removed: {sorted(removed)}"
    if sum(added.values()) > MAX_ADDED:
        return False, f"too many words added: {sorted(added)}"

    backed = {canon(v) for v in visible_words}          # alias-aware: "postgres" is backed by PostgreSQL
    unbacked_tech = [w for w in added if w in jd_words and w in tech_terms and canon(w) not in backed]
    if unbacked_tech:
        return False, f"technology terms not on your visible resume: {unbacked_tech}"
    non_jd = [w for w in added.elements() if w not in jd_words and w not in CONNECTORS]
    not_linking = [w for w in non_jd if w not in LINKING_WORDS]
    if not_linking:
        return False, f"added words not in the JD and not linking words: {sorted(set(not_linking))}"
    if len(non_jd) > MAX_NON_JD_WORDS:
        return False, f"more than {MAX_NON_JD_WORDS} added words that are not in the JD: {non_jd}"
    return True, "ok"


def check_edit(original: str, edited: str, jd_words: set[str],
               bank_words: set[str] | None = None) -> tuple[bool, str]:
    problem = _structure_problem(original, edited)
    if problem:
        return False, problem

    old, new = Counter(words(original)), Counter(words(edited))
    removed, added = old - new, new - old
    if sum(removed.values()) > MAX_REMOVED:
        return False, f"too many original words removed: {sorted(removed)}"
    if sum(added.values()) > MAX_ADDED:
        return False, f"too many words added: {sorted(added)}"
    not_in_jd = [w for w in added if w not in CONNECTORS and w not in jd_words]
    if not_in_jd:
        return False, f"added words not found in the JD: {not_in_jd}"
    if bank_words is not None:
        unbacked = [w for w in added if w not in CONNECTORS and w not in bank_words]
        if unbacked:
            return False, f"added terms not backed by anything on your resume: {unbacked}"
    return True, "ok"


def bank_vocabulary(data: dict) -> set[str]:
    """Every word you have written anywhere in the content bank or skills."""
    chunks = [" ".join(s["items"]) for s in data["skills"]]
    for section in ("experience", "projects"):
        for e in data[section]:
            chunks.append(e.get("stack", "") + " " + e.get("position", ""))
            chunks += [b["text"] for b in e["bullets"]]
    chunks += [c["text"] for c in data.get("cocurricular", [])]
    vocab = set(words(" ".join(chunks)))
    # also allow the pieces of compound terms ("Git/GitHub" -> git, github)
    vocab |= {part for w in vocab for part in re.split(r"[/.-]", w) if part}
    return vocab


def validate_plan(data: dict, plan: dict, jd_text: str) -> tuple[dict, list[str]]:
    """Returns (clean_plan, warnings)."""
    warnings: list[str] = []
    jd_words = set(words(jd_text))
    bank_words = bank_vocabulary(data)

    exp_by_id = {e["id"]: e for e in data["experience"]}
    proj_by_id = {p["id"]: p for p in data["projects"]}
    cc_ids = {c["id"] for c in data.get("cocurricular", [])}
    originals, conflicts = {}, {}
    for section in (data["experience"], data["projects"]):
        for e in section:
            for b in e["bullets"]:
                originals[b["id"]] = b["text"]
                conflicts[b["id"]] = set(b.get("conflicts_with", []))
    for c in data.get("cocurricular", []):
        originals[c["id"]] = c["text"]

    used: set[str] = set()

    def clean_entries(entries, by_id, label):
        out, seen = [], set()
        for e in entries or []:
            eid = e.get("id")
            if eid not in by_id or eid in seen:
                warnings.append(f"{label}: dropped unknown/duplicate entry '{eid}'")
                continue
            seen.add(eid)
            valid_ids = {b["id"] for b in by_id[eid]["bullets"]}
            bullets = []
            for bid in e.get("bullets", []):
                if bid not in valid_ids:
                    warnings.append(f"{eid}: dropped bullet '{bid}' (not in this entry)")
                elif bid in used:
                    warnings.append(f"{eid}: dropped duplicate bullet '{bid}'")
                elif conflicts[bid] & used:
                    warnings.append(f"{eid}: dropped '{bid}' (overlaps {sorted(conflicts[bid] & used)})")
                else:
                    bullets.append(bid)
                    used.add(bid)
            if not bullets:
                bullets = [b["id"] for b in by_id[eid]["bullets"] if b.get("default")][:1]
                used.update(bullets)
                warnings.append(f"{eid}: no valid bullets chosen, using its first default bullet")
            out.append({"id": eid, "bullets": bullets})
        return out

    experience = clean_entries(plan.get("experience"), exp_by_id, "experience")
    present = {e["id"] for e in experience}
    for e in data["experience"]:
        if e.get("required") and e["id"] not in present:
            warnings.append(f"required entry '{e['id']}' was missing — added with default bullets")
            experience.append({"id": e["id"],
                               "bullets": [b["id"] for b in e["bullets"] if b.get("default")]})

    projects = clean_entries(plan.get("projects"), proj_by_id, "projects")
    cocurricular = [c for c in plan.get("cocurricular", []) if c in cc_ids]

    # Skills: reorder only — must be the exact same set per line.
    skills = {}
    for s in data["skills"]:
        proposed = plan.get("skills", {}).get(s["label"])
        if proposed and sorted(proposed) == sorted(s["items"]):
            skills[s["label"]] = proposed
        else:
            if proposed:
                warnings.append(f"skills '{s['label']}': change rejected (reorder only) — kept original")
            skills[s["label"]] = list(s["items"])

    shown = {b for e in experience + projects for b in e["bullets"]} | set(cocurricular)
    edits, edit_log = _check_edits(
        plan, originals, shown, warnings,
        lambda o, t: check_edit(o, t, jd_words, bank_words))

    clean = {"experience": experience, "projects": projects, "cocurricular": cocurricular,
             "skills": skills, "edits": edits, "edit_log": edit_log}
    return clean, warnings


def _check_edits(plan: dict, originals: dict, shown: set, warnings: list[str],
                 checker) -> tuple[dict, list[dict]]:
    """Every proposed bullet edit goes through `checker(original, edited) -> (ok, reason)`."""
    edits, edit_log = {}, []
    for bid, text in (plan.get("edits") or {}).items():
        if bid not in originals or bid not in shown:
            continue
        text = " ".join(str(text).split())
        if text == originals[bid]:
            continue
        ok, reason = checker(originals[bid], text)
        if ok:
            edits[bid] = text
            edit_log.append({"id": bid, "status": "applied", "before": originals[bid], "after": text})
        else:
            edit_log.append({"id": bid, "status": "rejected", "reason": reason,
                             "before": originals[bid], "after": text})
            warnings.append(f"edit to '{bid}' rejected: {reason}")
    return edits, edit_log


def validate_locked_plan(data: dict, base: dict, plan: dict, jd_text: str) -> tuple[dict, list[str]]:
    """Locked mode (the default): the entries and bullets are exactly those of `base`
    (render.default_plan). Claude may only reorder bullets within an entry, reorder
    skill items, and propose keyword edits. Returns (clean_plan, warnings).

    A bullet order is accepted only if it is an exact permutation of that entry's
    base bullets; anything else keeps the base order and logs a warning."""
    warnings: list[str] = []
    jd_words = set(words(jd_text))

    originals = {}
    for section in (data["experience"], data["projects"]):
        for e in section:
            for b in e["bullets"]:
                originals[b["id"]] = b["text"]
    for c in data.get("cocurricular", []):
        originals[c["id"]] = c["text"]

    # "Backed by your resume" means backed by what is actually on it. Words that only
    # occur in hidden (default: false / previously commented-out) content don't count,
    # or an edit could smuggle that content back in.
    visible_ids = {b for s in ("experience", "projects") for e in base[s] for b in e["bullets"]}
    visible_ids |= set(base["cocurricular"])
    visible = dict(data)
    for section in ("experience", "projects"):
        visible[section] = [
            {**e, "bullets": [b for b in e["bullets"] if b["id"] in visible_ids]}
            for e in data[section] if any(b["id"] in visible_ids for b in e["bullets"])]
    visible["cocurricular"] = [c for c in data.get("cocurricular", []) if c["id"] in visible_ids]
    bank_words = bank_vocabulary(visible)

    def lock(section: str) -> list[dict]:
        proposed = {e.get("id"): e.get("bullets") for e in (plan.get(section) or [])
                    if isinstance(e, dict)}
        base_ids = {e["id"] for e in base[section]}
        for eid in proposed:
            if eid not in base_ids:
                warnings.append(f"{section}: ignored entry '{eid}' (locked selection)")
        out = []
        for e in base[section]:
            order = proposed.get(e["id"])
            if order is None:
                warnings.append(f"{e['id']}: no order proposed — kept base order")
                order = e["bullets"]
            elif not isinstance(order, list) or sorted(order) != sorted(e["bullets"]):
                warnings.append(f"{e['id']}: bullet order rejected (must be an exact "
                                f"permutation of the base bullets) — kept base order")
                order = e["bullets"]
            out.append({"id": e["id"], "bullets": list(order)})
        return out

    experience, projects = lock("experience"), lock("projects")
    cocurricular = list(base["cocurricular"])

    skills = {}
    for s in data["skills"]:
        proposed = (plan.get("skills") or {}).get(s["label"])
        if proposed and sorted(proposed) == sorted(s["items"]):
            skills[s["label"]] = proposed
        else:
            if proposed:
                warnings.append(f"skills '{s['label']}': change rejected (reorder only) — kept original")
            skills[s["label"]] = list(s["items"])

    shown = {b for e in experience + projects for b in e["bullets"]} | set(cocurricular)
    tech_terms = jd_tech_terms(jd_text)
    edits, edit_log = _check_edits(
        plan, originals, shown, warnings,
        lambda o, t: check_edit_locked(o, t, jd_words, tech_terms, bank_words))

    clean = {"experience": experience, "projects": projects, "cocurricular": cocurricular,
             "skills": skills, "edits": edits, "edit_log": edit_log}
    return clean, warnings
