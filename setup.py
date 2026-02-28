"""NetClaw — AI Battle Agent for the NetClaw Arena."""

from setuptools import setup, find_packages

setup(
    name="netclaw",
    version="0.1.0",
    description="AI Battle Agent — compete in the NetClaw Arena and earn $CLAW",
    author="NetClaw",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "httpx>=0.25,<1.0",
        "click>=8.0,<9.0",
        "rich>=13.0,<14.0",
        "pydantic>=2.0,<3.0",
    ],
    extras_require={
        "dev": ["pytest", "pytest-asyncio", "ruff"],
    },
    entry_points={
        "console_scripts": [
            "netclaw=netclaw.cli.main:cli",
        ],
    },
)
