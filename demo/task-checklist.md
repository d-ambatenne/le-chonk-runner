# Task checklist

The triage agent checks every new task against this list before it creates the issue. Edit the list to change
the rules: the agent gets the new version with the next message.

1. **One outcome.** The task asks for one feature or one fix, and it fits in a sentence.
2. **Acceptance criteria.** One to five statements that someone can check in the Android app or with a unit test.
3. **Small.** A coding agent can do it in one session: no rewrite, no new platform, no change of the build setup.
4. **Testable here.** It can be checked in the Android app or with the shared tests: the container can't build iOS.
5. **In scope.** It's about the code of this project: no secrets, accounts, payments or outside services.
6. **New.** No open issue asks for the same outcome.

When a request misses something, propose the missing part, for example acceptance criteria drawn from the
request, and ask the person to confirm it.
