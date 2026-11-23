## What the mutation showed

R2 — the mid-life fix landed and the mutation was re-measured:

- *S1* the mid-life test was vacuous. Removing `HkQrCode`'s `QrNotReadyError`
  catch now reddens that test and leaves the other cases alone, so the mutation
  is the proof the pin is load-bearing.

Measured after the fix: the catch reddens that test and the other cases stay
(1 failed / 5 passed), which is the tally quoted in the evidence block.
