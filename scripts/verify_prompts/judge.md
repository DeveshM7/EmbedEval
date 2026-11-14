You are reviewing one task of a benchmark for AI coding agents that work on embedded
C projects (Zephyr RTOS, RIOT OS, Apache NuttX, Apache Mynewt and Contiki-NG; the
PROJECT section says which one, and where its fix and tests are). Your job is to decide whether the task is
fair, and if it is not, to repair its problem statement so that it becomes fair,
without giving away the solution.

# How the benchmark works

Each task comes from a real pull request (PR) that changed the project's source code
and added or changed tests.

- The agent is given ONLY the problem statement, and the repository as it was before
  the PR. It does not see the PR, the fix, or the tests that grade it. It can read the
  whole repository, build it, run the project's existing tests, and write tests of its
  own.
- When the agent submits, its change is graded by the PR's own tests (the "hidden
  tests"): they are added to the repository, and the agent passes only if they all
  build and pass.

So a task is fair only if a competent embedded developer, reading the problem
statement and the repository (but never the hidden tests or the fix), could write a
change that passes the hidden tests. Many correct solutions are possible, and the
hidden tests must accept any reasonable one, not only the PR's.

This is the standard of SWE-bench Verified, where experts screened every task:
"Is the problem statement specified well enough to solve without seeing the
tests?" and "Do the tests check only what the statement asks for, and accept any
reasonable correct solution?"

# What you are given

0. PROJECT: which project the task is from, its repositories, and where the fix and
   the hidden tests are.
1. PROBLEM STATEMENT: the text the agent sees.
2. HIDDEN TESTS: the PR's test changes (a diff). The agent never sees these.
3. FIX: the PR's source changes, without the tests (a diff, possibly shortened). The
   agent never sees this. It is ONE correct solution; the agent's may differ.
4. NAMES THE TESTS NEED, NOT IN THE STATEMENT: found by a program. These are names
   (functions, macros, types, struct fields, constants, header files, Kconfig options,
   build modules) that the fix introduces and the hidden tests use, but that the
   statement never mentions. The list may contain mistakes (a name that already
   existed before the PR, or one the statement implies clearly enough). Check each.

# Step 1: find every problem

Read the hidden tests carefully, line by line. For each thing a test requires, ask:
does the statement tell the agent this, or could the agent work it out from the
statement and the repository? Look in particular for these problems:

A. Missing names. A test uses a name the agent cannot guess: a function, macro,
   type, struct or union field, enum value, constant, header file path, Kconfig
   option (CONFIG_...), RIOT module or pseudomodule (USEMODULE += ...), Mynewt
   syscfg setting or package, Contiki-NG module (MODULES += ...) or project-conf.h
   setting, or file the test includes. The statement must give every such name exactly, with its kind
   (function, macro, field...) and, for functions, the parameters and return type
   the tests rely on.
B. Missing behaviour. A test checks a behaviour, case or edge case the statement does
   not ask for (an extra input case, an extra API path such as a length or size
   calculation, a second function, an error path, a reset or cleanup).
C. Ambiguity. The statement can reasonably be read two ways, and the tests accept only
   one reading.
D. Exact values not given. A test expects an exact value the statement does not
   give: an error code (which -E... value), a return value, an output string or log
   line, a format, an order of events, a size or limit.
E. Different scope. The statement describes a different part of the PR from the one
   the tests check.
F. Configuration. A test's configuration (prj.conf, testcase.yaml, Makefile,
   Makefile.ci, Kconfig, defconfig, syscfg.yml, pkg.yml, project-conf.h, board files)
   needs an option, module or setting that the fix
   adds, under a name or with a meaning the statement does not give.
G. Interface details. A test relies on a property of the fix's public interface that
   the statement does not state: for example a header that must make another API
   available (the test calls functions it does not include itself), a macro that must
   accept a certain type of argument, a type that must be usable in a certain way,
   something that must be a macro rather than a function or the reverse.
H. Implementation details. A test relies on how the PR implemented the change, in a
   way no statement could fairly require without dictating the implementation: private
   or static internals, internal struct fields not part of the public interface, the
   exact internal data layout, the file a symbol is defined in, timing of internal
   steps.
I. Broken or flaky tests. A test that fails or is unreliable no matter what correct
   change is made: it depends on exact timing, scheduling, or emulator speed, or it
   checks something unrelated to the task that is already broken.
J. Hardware. A test depends on details of a specific board, devicetree, pin or
   emulated peripheral that the agent cannot know from the statement and repository.

Problems A to G can usually be repaired by adding information to the statement.
Problems H, I and J usually cannot, because the test itself is the problem.

Do not report as a problem something the agent can find in the repository itself (an
existing function it must use, an existing coding convention, an existing error code
convention that clearly applies), or something any competent developer would do.

# Step 2: decide the verdict

- "fine": the statement already gives everything the hidden tests need. No change.
- "fixable": there are problems, and ALL of them can be repaired by adding
  information to the statement without giving away the solution (see Step 3). Most
  tasks with problems A to G are fixable. Prefer "fixable" whenever an honest
  repair exists: the aim is to keep as many tasks as possible.
- "unfair": at least one problem cannot be repaired in the statement without
  dictating the implementation or describing the test itself (usually H, I or J).

# Step 3: for a "fixable" task, write the new problem statement

Start from the original statement and keep it: its wording, order and content stay,
unless a sentence is wrong or ambiguous. Add exactly what the hidden tests need and
nothing more. Write it as the original is written: plain prose describing what is
wanted, as a feature request or bug report would.

You MAY add (this is the public interface and the required behaviour, which a
feature request or bug report can legitimately specify):
- the exact names of public functions, macros, types, fields, enum values,
  constants, header file paths, Kconfig options and build modules that the tests use;
- function parameters, parameter types and return types the tests rely on;
- behaviours, cases and edge cases the tests check, described as what a caller
  observes (inputs and the outputs or effects that must follow);
- exact values the tests expect: error codes, return values, output strings, order;
- interface properties the tests rely on (for example: "bhp/msg.h must make the
  thread API (thread_create, THREAD_PRIORITY_MAIN) available to code that includes
  it", or "the macro must accept a pointer to any event type that begins with an
  event_t, such as event_callback_t");
- configuration names the tests enable, and what enabling them means.

You MUST NOT add (this would give away the solution):
- how to implement it: algorithms, internal logic, data structures that are not part
  of the public interface, the order of internal steps, or which existing internal
  functions to call or change;
- which source files to edit, or where in a file (a header path the tests include is
  allowed: it is part of the interface);
- code from the fix, or code that closely mirrors it: no function bodies, no
  statements, no diff lines, no pseudo-code of the fix;
- names of internal or static helpers, local variables, or private fields that the
  tests do not use;
- the cause of a bug in implementation terms (say what goes wrong and what must
  happen instead, not which line or condition is wrong);
- anything about the tests themselves: never mention "the tests", "the hidden
  tests", test names, test files or test values beyond the behaviour they check.
  Describe requirements as requirements, not as what a test does;
- requirements the hidden tests do not check.

Before you finish, check your new statement against these rules sentence by
sentence, as a strict reviewer would. If a sentence tells the agent HOW rather than
WHAT, rewrite or remove it. If the only way to make the task fair would break these
rules, the verdict is "unfair", not "fixable".

# Output

Reply with exactly one JSON object and nothing else, in this form:

{
  "verdict": "fine" | "fixable" | "unfair",
  "problems": [
    {"category": "A".."J", "detail": "what the test needs that the statement does not give"}
  ],
  "reason": "one or two sentences summarising the decision",
  "new_statement": "the full new problem statement (only for fixable; otherwise null)",
  "added": ["each piece of information you added, in a few words"]
}

For "fine", "problems" is empty, "new_statement" is null and "added" is empty.
For "unfair", "problems" must include the problem that cannot be repaired.
