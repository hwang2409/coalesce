# coalesce vision

## what coalesce is

coalesce is an open-source local server that exposes CAD as MCP tools to a
user's own agent harness. It helps a mechanical engineer get a rigorous v0 of
an idea out quickly, with output that can continue in their main CAD workflow
as a clean STEP file.

The user starts the local server and connects its MCP to the agent harness they
already use, such as Claude Code, Codex, or Cursor. A local browser viewer lets
the human watch and inspect the model live as it takes shape.

## what coalesce is not

coalesce is not a hosted product, and it is not an agent harness. It is an
extension of the user's native harness, so the user can keep the context and
workflow they already prefer.

## the first domain

The first test domain is electronics enclosures around a PCB, with FDM
printing first. The scope then widens to general mechanical v0 parts.

Basic multi-part assemblies are planned for v1.

## the rigor bar

The core loop should have the quality and rigor of Fusion 360: constrained
sketches, a parametric timeline, core solid features, inspection, and reliable
output. The core operations include extrude, revolve, sweep, loft, fillet,
chamfer, shell, draft, holes, patterns, and mirror, along with multi-body
work.

Exact geometric checks are the primary feedback. Renders support those checks.
The design should produce structured, actionable errors rather than silently
choosing an uncertain result.

Later work includes sheet metal, drawings, CAM, and simulation.

## design principles

- use a typed feature graph as the source of truth;
- resolve history-based references from feature provenance, not indices;
- make exact geometric checks the primary feedback;
- return structured, actionable errors;
- make edits idempotent, with expected revisions and request IDs.

## in henry's words

> my general idea for coalesce is something very similar to dingcat, but for production ready cad utilizing agent agents. what it essentially can be is simply an mcp that allows for the effective transport of information through tools, so that agents have more flexibility with cad-ing, since most of the current cad softwares are built with engineers first, agents are more of an afterthought. i think that an mcp is the right general idea to approach this because ultimately we don't want this product to be something that we sell, its more of an extension of peoples native harnesses. whatever they prefer to use for their engineering work, maybe they're ideating with a claude code session and they want to cad it up to test if an idea will work. rather than forcing users to use a custom product built harness with a model that has no context, they just spin up the local server (which starts a server for the mcp or something) and then they can connect the mcp to their existing agent run. i think thats kind of what i think this should be. in my mind, its a working product that real mechanical engineers would use to get a v0 of an idea out, something that is seamless and smooth, but still requires rigor for the standard cad-ing procedure. does that make sense? the capabilities of the cad software itself should be of similar quality and rigor to something like fusion 360
