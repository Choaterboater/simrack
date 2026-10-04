"""Built-in fabric recipes: a campus IP-Clos and smaller cuts of it.

Their subnets are fixed sandbox ranges; a fabric build refuses any that overlap
a subnet the lab profile protects.
"""

from __future__ import annotations

from .models import Network, Recipe

def ip_clos_sandbox() -> Recipe:
    """2 cores + 2 access, exactly the live topology, on sandbox subnets."""
    return Recipe(
        name="ip-clos",
        description="Campus IP-Clos: 2 core + 2 access, data/voice VLANs, border to a vSRX.",
        site_name="Sandbox",
        networks=[
            Network(name="data", vlan=10, cidr="10.60.10.0/24", gateway="10.60.10.1"),
            Network(name="voice", vlan=20, cidr="10.60.20.0/24", gateway="10.60.20.1"),
        ],
        vrf="LAB",
        overlay_as=65200,
        underlay_cidr="10.255.224.0/20",
        loopback_cidr="172.31.0.0/23",
        mgmt_network="management",
        roles=[
            {"name": "sbx-core-01", "role": "core", "ports": 4},
            {"name": "sbx-core-02", "role": "core", "ports": 4},
            {"name": "sbx-acc-01", "role": "access", "ports": 4},
            {"name": "sbx-acc-02", "role": "access", "ports": 4},
        ],
    )


def collapsed_core_sandbox() -> Recipe:
    """One core doing core and leaf, plus one access. Good first sandbox: 10 GB."""
    recipe = ip_clos_sandbox()
    recipe.name = "collapsed-core"
    recipe.description = "1 core + 1 access. Smallest useful sandbox (~10 GB)."
    recipe.roles = [
        {"name": "sbx-core-01", "role": "core", "ports": 4},
        {"name": "sbx-acc-01", "role": "access", "ports": 4},
    ]
    return recipe


def single_switch_sandbox() -> Recipe:
    """One switch, no cabling. Useful for image and config work, ~5 GB."""
    recipe = ip_clos_sandbox()
    recipe.name = "single-switch"
    recipe.description = "One access switch, no links. The cheapest sandbox."
    recipe.roles = [{"name": "sbx-acc-01", "role": "access", "ports": 4}]
    return recipe


RECIPES = {
    "ip-clos": ip_clos_sandbox,
    "collapsed-core": collapsed_core_sandbox,
    "single-switch": single_switch_sandbox,
}


def get_recipe(name: str) -> Recipe:
    if name not in RECIPES:
        raise KeyError(name)
    recipe = RECIPES[name]()
    if not recipe.site_name:
        recipe.site_name = name
    return recipe


def list_recipes() -> list[dict]:
    return [
        {
            "name": name,
            "description": get_recipe(name).description,
            "roles": [{"name": r["name"], "role": r["role"]} for r in get_recipe(name).roles],
        }
        for name in sorted(RECIPES)
    ]
