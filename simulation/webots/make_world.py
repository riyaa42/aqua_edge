"""Write worlds/aqua_edge.wbt: building B in 3D with one sensor node per flat, a tank node, a gateway.

Node positions use real heights: floor f taps at 3f + 1 m, flats at the 4 corners of each floor.
Re-run after changing the node list (data/sensors.csv).
"""
import math
import os
from simlink import SensorFeed

HERE = os.path.dirname(os.path.abspath(__file__))
FLOOR_H, FLOORS, ROOF = 3.0, 5, 15.0
CORNER = {1: (-6, -6), 2: (6, -6), 3: (6, 6), 4: (-6, 6)}
GATEWAY_AT = (16, -14, 1.0)


def look_at(pos, target):
    """Webots Viewpoint orientation (axis-angle) looking from pos to target, z up."""
    dx, dy, dz = (t - p for t, p in zip(target, pos))
    yaw, pitch = math.atan2(dy, dx), math.atan2(-dz, math.hypot(dx, dy))
    cy, sy, cp, sp = math.cos(yaw / 2), math.sin(yaw / 2), math.cos(pitch / 2), math.sin(pitch / 2)
    w, x, y, z = cy * cp, -sy * sp, cy * sp, sy * cp            # q = Rz(yaw) * Ry(pitch)
    angle = 2 * math.acos(w)
    s = math.sqrt(max(1 - w * w, 1e-12))
    return f"{x / s:.4f} {y / s:.4f} {z / s:.4f} {angle:.4f}"


def solid(name, xyz, geometry, colour, transparency=0.0):
    return (f'Solid {{ name "{name}" translation {xyz[0]} {xyz[1]} {xyz[2]} children [ Shape {{ '
            f'appearance PBRAppearance {{ baseColor {colour} roughness 0.8 metalness 0 transparency {transparency} }} '
            f'geometry {geometry} }} ] }}')


def robot(name, xyz, controller, size, supervisor=False):
    return f'''Robot {{
  name "{name}"
  translation {xyz[0]} {xyz[1]} {xyz[2]}
  controller "{controller}"
  supervisor {"TRUE" if supervisor else "FALSE"}
  children [
    LED {{
      name "led"
      color [0 0.8 0, 1 0.75 0, 1 0 0]
      children [ Shape {{ appearance PBRAppearance {{ baseColor 0.25 0.25 0.25 roughness 0.5 metalness 0 }} geometry Box {{ size {size} {size} {size} }} }} ]
    }}
    Emitter {{ name "emitter" type "radio" channel 1 range 200 }}
    Receiver {{ name "receiver" type "radio" channel 1 }}
  ]
}}'''


def flat_xyz(name):                 # "B-203" -> floor 2, flat 3
    code = name.split("-")[1]
    floor = 0 if code[0] == "G" else int(code[0])
    x, y = CORNER[int(code[-1])]
    return (x, y, floor * FLOOR_H + 1.0)


if __name__ == "__main__":
    nodes = SensorFeed().nodes()
    parts = [
        "#VRML_SIM R2025a utf8",
        'WorldInfo { info ["Aqua Edge: building B, 20 flat nodes, tank node, gateway"] title "Aqua Edge" basicTimeStep 32 }',
        f"Viewpoint {{ orientation {look_at((42, -36, 26), (0, 0, 7))} position 42 -36 26 }}",
        "Background { skyColor [0.75 0.85 0.95] }",
        "DirectionalLight { direction -0.4 0.6 -1 intensity 2.5 castShadows FALSE }",
        solid("ground", (0, 0, 0), "Plane { size 80 80 }", "0.55 0.62 0.5"),
    ]
    for f in range(FLOORS + 1):                                   # floor slabs, see-through
        parts.append(solid(f"slab{f}", (0, 0, f * FLOOR_H), "Box { size 18 18 0.15 }", "0.85 0.85 0.8", 0.7))
    parts.append(solid("downtake", (0, 0, ROOF / 2 + 1), f"Cylinder {{ height {ROOF + 1} radius 0.15 }}", "0.2 0.4 0.9"))
    parts.append(solid("tank", (0, 0, ROOF + 1.2), "Cylinder { height 2 radius 1.5 }", "0.15 0.2 0.3", 0.3))
    for n in nodes:
        if n.endswith("TANK"):
            parts.append(robot(n, (2.2, 0, ROOF + 1.2), "aqua_node", 0.9))
        else:
            parts.append(robot(n, flat_xyz(n), "aqua_node", 0.7))
    parts.append(solid("gateway_pole", (GATEWAY_AT[0], GATEWAY_AT[1], 0.5), "Cylinder { height 1 radius 0.1 }", "0.4 0.4 0.4"))
    parts.append(robot("GATEWAY", (GATEWAY_AT[0], GATEWAY_AT[1], GATEWAY_AT[2] + 0.5), "aqua_gateway", 1.2, supervisor=True))
    os.makedirs(os.path.join(HERE, "worlds"), exist_ok=True)
    with open(os.path.join(HERE, "worlds", "aqua_edge.wbt"), "w") as f:
        f.write("\n".join(parts) + "\n")
    print(f"wrote worlds/aqua_edge.wbt with {len(nodes)} nodes + gateway")
