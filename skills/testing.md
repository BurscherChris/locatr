# Testing

Always inspect existing tests before changing behavior. Add or update tests for meaningful behavior changes. Prefer focused tests first. Run the relevant test suite after implementation. Run broader tests when practical. Do not fake successful test results. Do not delete or weaken tests to make the suite green. Distinguish code failures from environment/infrastructure failures. Report exact validation performed.

For Python projects: pytest, unit tests, integration tests where appropriate. For frontend: use the project's existing test framework. Do not introduce a new framework unnecessarily.