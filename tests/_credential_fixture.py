"""Explicit legacy fixture aliases; shared tools have no built-in repository map."""
import json


ALIASES = {
    "Qatlib": ["LAKE_PUBLIC_CACHE_KEY", "LAKE_CACHE_KEY"],
    "ConsumerLib": "LAKE_DEV_CACHE_KEY",
    "consumer_lib": "LAKE_DEV_CACHE_KEY",
    "SecondaryProject": "LAKE_SECONDARY_CACHE_KEY",
    "secondary_package": "LAKE_SECONDARY_CACHE_KEY",
}


def environment(values=None):
    return {"QATLIB_CACHE_KEY_VARIABLES": json.dumps(ALIASES), **(values or {})}
