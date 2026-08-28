# Order Service Demo Fixture

This directory is the immutable source for the multi-file demonstration. Do not
run the agent directly against `fixture/`; create a disposable copy first:

```powershell
python scripts/reset-demo.py
mini-coder --workspace examples/order_service/workspace --auto "Read TASK.md and complete it."
```

`workspace/` is ignored by the parent repository. Running the reset command
removes only that generated directory, copies `fixture/` back into it, and (when
Git is installed) creates an independent clean Git baseline so Agent status
commands cannot walk up into the parent repository.
