import os

# Tests must never hit the network: disable the free CAD home-age resolver
# (report cards fall back to the ServiceTitan custom-field value).
os.environ.setdefault("LEX_DISABLE_FREE_CAD_HOME_AGE", "1")
# And never let a developer's OpenRouter key turn unit tests into live LLM calls.
os.environ.setdefault("LEX_REPORT_CARD_USE_LLM", "0")
