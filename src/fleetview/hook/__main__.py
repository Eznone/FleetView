"""So the generated hooks config can invoke `python -m fleetview.hook`."""

from fleetview.hook.shim import main

raise SystemExit(main())
