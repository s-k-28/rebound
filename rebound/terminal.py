"""Input guards for local signing commands."""

import getpass
import warnings


def read_hidden_key(prompt: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise ValueError("A terminal that can hide input is required. No key was read.") from None
