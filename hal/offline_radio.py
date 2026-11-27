"""For tests and the offline run, whose fake radio takes no time: step the gateway, then let
every node that was sent something answer, until the air is quiet. On the real radio these
exchanges take seconds, so a stored backlog empties well within one simulated minute."""


def exchange(nodes, gateway, inboxes):
    """nodes: node objects; inboxes: {name: [packets waiting]} as filled by the fake radio."""
    gateway.step()
    for _ in range(10_000):
        waiting = [n for n in nodes if inboxes.get(n.id)]
        if not waiting:
            return
        for n in waiting:
            n.radio_poll()
        gateway.step()
    raise RuntimeError("the fake radio never went quiet")
