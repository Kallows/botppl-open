# Checkpoints

This folder will hold signed checkpoint roots of the botppl vault: a periodic
hash over every stored version, signed by the service. Because this
repository's history is public and anyone can clone it, a published root
cannot be quietly rewritten later. An agent that kept a receipt can check it
against the root of its day.

No checkpoint has been published yet. The format and signing key will be
documented here with the first one.
