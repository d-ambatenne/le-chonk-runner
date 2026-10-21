# Le Chonk Runner

Build "Le Chonk Runner", a small endless-runner arcade game for Android and iOS, as a Kotlin Multiplatform project built with the Kotlin Toolchain (JetBrains' `kotlin` command-line tool). Do not use Gradle or Maven.

Work in the current directory, `/app`. The `kotlin` command is installed. This machine runs Linux, so it can't build or run the iOS app, which needs macOS with Xcode: set the iOS app up correctly anyway, and check your work by building the Android app and running the tests.

## The game

- Le Chonk, a round, chonky cat, runs automatically along the ground; the world scrolls towards him.
- Obstacles come from the right in random order, with gaps that are always passable:
  - low obstacles on the ground, which Le Chonk must jump over;
  - high obstacles at head height, which Le Chonk must sit down under.
- Controls: tap or swipe up to jump; swipe down, or press and hold the lower half of the screen, to sit. Le Chonk stands up again when the finger is lifted.
- The score grows with the distance run and is shown on screen. The speed increases slowly over time.
- Touching an obstacle ends the game and shows the final score; a tap starts a new game.
- Draw everything with Compose: Canvas and simple shapes, no game engine, images or other assets.

## Structure and quality

- Put the game logic and the Compose UI in a shared module, in common code. The Android app and the iOS app both show the same shared `@Composable`.
- Keep the game logic (physics, spawning, collisions, scoring) free of UI code, and cover it with unit tests in the shared module's common tests: a jump returns Le Chonk to the ground, sitting lowers his hitbox so a high obstacle passes, hitting a low obstacle ends the game, the score increases over time, and spawning is reproducible with a fixed seed.
- The Android app must build to an APK, and all tests must pass.

When you are done, reply with a short summary and the commands that build the Android app and run the tests.
