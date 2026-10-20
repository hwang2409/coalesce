# LLM / agent CAD research survey (2023-2026) and eval proposal for coalesce

Status: complete (first pass). Sections: 1 papers, 2 datasets, 3 failure modes vs coalesce features, 4 visual vs geometric feedback, 5 eval proposal, 6 unverified items.

Conventions: each claim has a URL. "Inference:" marks my own reasoning, not a paper result. "Not found" means I searched and did not find the detail.

## 1. Papers

### 1.1 CADCodeVerify + CADPrompt — "Generating CAD Code with Vision-Language Models for 3D Designs" (Alrashedy et al., ICLR 2025)
- URL: https://arxiv.org/abs/2410.05340 (HTML v2: https://arxiv.org/html/2410.05340v2), code/data: https://github.com/Kamel773/CAD_Code_Generation
- Task: natural-language prompt -> CadQuery Python code -> 3D object; then refine the code.
- Representation: CadQuery code. They chose CadQuery over OpenSCAD because it is Python (more training data) and "design-intent" code is shorter (App. B.1).
- Feedback: VLM renders the object, generates validation questions from the prompt, answers them from the images, and writes corrective feedback. Compile errors are also fed back (re-submit code + compiler error at temperature 1, App. B.3). Two refinement rounds; "no improvement was observed beyond the second refinement" (App. B.3).
- Baselines: 3D-Premise (image straight to VLM) and a "geometric solver" baseline (FreeCAD computes 13 categories of geometric properties of generated vs ground-truth object, LLM verbalizes the difference). The geometric solver needs the ground truth, so the authors call it an upper bound (Sec. 5.1).
- Benchmark CADPrompt: 200 prompts + expert-written CadQuery + ground-truth meshes, split by mesh complexity, compile difficulty, geometric complexity (Sec. 4).
- Metrics: point-cloud (Chamfer-like) distance, Hausdorff distance, IoGT (bounding-box overlap over ground truth), compile rate. Failed compiles get max distance sqrt(3) and IoGT 0 (Sec. 5.2).
- Headline results (Table 2, median): GPT-4 zero-shot point-cloud distance 0.153 generated -> 0.132 CADCodeVerify -> 0.102 geometric solver*. Compile rate: GPT-4 96.5%, Gemini 85%, CodeLlama 73.5%. CADCodeVerify: 7.30% reduction in point-cloud distance and +5.5% compile rate vs prior work for GPT-4 (abstract). On "Hard" split, 3D-Premise image feedback dropped compile rate by 20 points to 62%; CADCodeVerify raised it about 9 points (Sec. 6).
- Ablation: removing reference images in refinement worsens point-cloud distance 0.126 -> 0.153 (Sec. 6).
- Failure modes (App. C.2, 50 samples, 3 annotators): structural configuration error 48% (largest), logical error 18%, plus spatial-precision (dimension) errors and compile failures. Feedback types (App. C.1): structural 52% -> 38% across refine rounds, dimensional 20% -> 26%, plus positional (alignment) feedback.
- Limitations they state: point-cloud and Hausdorff distances are noisy and miss structural defects (e.g. small gaps between desk legs and top); prompts are ambiguous (functional vs geometric wording) (Sec. 7).
- Note for coalesce (inference): in their own table, ground-truth geometric feedback beat visual feedback on distance. Visual feedback is what you can use when no ground truth exists. Requirements-as-tests give a ground-truth-free middle path: exact geometric checks against the stated spec, not against a hidden target.

### 1.2 Text2CAD (Khan, Sinha et al., NeurIPS 2024)
- URL: https://arxiv.org/abs/2409.17106 (HTML: https://arxiv.org/html/2409.17106), project: https://sadilkhan.github.io/text2cad-project/
- Task: text prompt -> parametric CAD construction sequence. Prompts at 4 levels: abstract (L0), beginner (L1), intermediate (L2), expert (L3, with coordinates and extrusion depths).
- Representation: DeepCAD-style sketch-and-extrude token sequence (lines, arcs, circles, extrusions). Not code. Trained transformer (not a general LLM agent).
- Data: about 170K DeepCAD models and 660K text annotations, made by Mistral + LLaVA-NeXT from multi-view renders (abstract).
- Feedback: none (single-shot autoregressive generation).
- Metrics: F1 of primitives and extrusions (Hungarian matching of loops), Chamfer distance (CD), invalidity ratio (IR), GPT-4V pairwise visual verdict, user study with 5 CAD designers (Sec. 5.1).
- Headline results (Table 1, expert prompts only): Text2CAD F1 line 81.13 / arc 36.03 / circle 74.25 / extrusion 93.31; median CD 0.37 (x1e3) vs DeepCAD baseline 32.82; IR 0.93% vs 10.00%.
- Failure modes (Sec. 15): (1) invalidity about 1%: degenerate parameters (same start and end point, zero extrusion depth); (2) discrepancy: output does not match prompt, more common when the prompt names an object ("spatula", "paddle") instead of geometry; they blame noisy VLM annotations. Arcs are the weakest primitive (F1 36). Dataset is biased to rectangles and cylinders (Sec. 6).
- Note for coalesce (inference): DeepCAD-derived text prompts are machine-written from renders and are noisy; do not use them as ground-truth requirements without human review.

### 1.3 CAD-Recode (Rukhovich et al., ICCV 2025)
- URL: https://arxiv.org/abs/2412.14042 (HTML: https://arxiv.org/html/2412.14042), code: https://github.com/filaPro/cad-recode
- Task: point cloud -> executable CadQuery Python code (CAD reverse engineering).
- Representation: CadQuery sketch-extrude code. Model: point-cloud projector + Qwen2-1.5B decoder (abstract, Sec. 4.2).
- Data: 1M procedurally generated CadQuery programs (Sec. 3.2). Evaluated on DeepCAD, Fusion 360 and real-scan CC3D test sets.
- Feedback: none in the loop. Test-time sampling (several point-cloud samplings, pick a valid result) mainly lowers invalidity (Sec. 5, Table 3).
- Metrics: Chamfer distance (x1e3, 8192 points), IoU of meshes (%), invalidity ratio (IR) (Sec. 5.1).
- Headline results (Table 1): trained on its 1M set, DeepCAD test mean CD 0.30, IoU 92.0%, IR 0.4%; Fusion 360 test mean CD 0.35, IoU 87.8%, IR 0.5%. Previous best CAD-SIGNet: DeepCAD IoU 77.6%, Fusion 360 IoU 65.6%.
- Code is LLM-readable: CAD-Recode code + GPT-4o gives 76.5% accuracy on the point-cloud version of SGP-Bench CAD question answering (Sec. 5.2). They also show editing of the code by an off-the-shelf LLM (App. G).
- Failure modes: not reported in a structured way (not found). Scope is sketch-extrude only (Sec. 3.1).
- Note for coalesce (inference): procedural generation of training programs avoids leakage from public datasets and gives control over feature mix. The same idea works for generating eval variants.

### 1.4a CAD-Coder (image -> CadQuery; Doris, Alam, Nobari, Ahmed, MIT, 2025)
- URL: https://arxiv.org/abs/2505.14646 (HTML: https://arxiv.org/html/2505.14646), code: https://github.com/anniedoris/CAD-Coder
- Task: single image of a part -> CadQuery code.
- Representation: CadQuery. Dataset GenCAD-Code: 163K image/code pairs made by converting GenCAD (DeepCAD-derived sketch/extrude commands: line, arc, circle, extrude) to CadQuery (Sec. 3.1).
- Feedback: none (single-shot fine-tuned LLaVA-1.5-style VLM).
- Metrics: valid syntax rate (VSR) and IOU_best (scale-normalized by radius of gyration, principal-axis aligned, best of all valid alignments; computed only over scripts that run) (Sec. 3.3.2).
- Headline results (Table 2, 100 test samples): CAD-Coder VSR 100%, IOU_best 0.675; GPT-4.5 84% / 0.524; GPT-o1 88% / 0.494; Gemini-2.0-Pro 82% / 0.445; Qwen2.5-VL-72B 94% / 0.352.
- Failure modes (Sec. 5): wrong dimensions and proportions from images taken from varied perspectives; generalization to unseen CAD operations is "heavily prompt-dependent".
- Note: even frontier VLMs reach only about 0.5 IoU on simple DeepCAD-style parts from one image, which means single-image geometry reading is weak (inference from the table).

### 1.4b CAD-Coder (text -> CadQuery with CoT and geometric reward; Guan et al., Beihang/HKU, NeurIPS 2025)
- URL: https://arxiv.org/abs/2505.19713 (HTML: https://arxiv.org/html/2505.19713); NeurIPS listing: https://papers.neurips.cc/paper_files/paper/2025/hash/564a224e88f2490f3c1deaae877d37e3-Abstract-Conference.html
- Task: text -> CadQuery code.
- Representation: CadQuery. They argue it gives "inherent geometric validation" because the script can be run (Sec. 1).
- Data: 110K text/CadQuery/3D triplets + 1.5K chain-of-thought samples, built from the Text2CAD dataset by having DeepSeek-V3 write candidate CadQuery, executing it, and keeping the lowest-Chamfer script (Sec. 4).
- Feedback: used as a training signal, not at inference: GRPO reinforcement learning with Chamfer-distance reward + format reward.
- Metrics: mean and median Chamfer distance (x1e3), invalidity ratio.
- Headline results (Sec. 5): full method mean CD 6.54, median CD 0.17 (Qwen2.5-7B base). Ablation: SFT only mean CD 74.55; GRPO without CoT 17.34. So the geometric reward gave the largest gain.
- Failure modes (App. F): (a) multi-part structures with wrong spatial alignment between sub-parts (dislocations, offsets); (b) confusing extrude vs cut (boolean type); (c) thin walls and internal cavities, where sparse point sampling allows "reward hacking". App. E: simple edits (change a dimension, remove a component, change translation/rotation) work without edit training.
- Note for coalesce (inference): Chamfer reward can be hacked on thin or hollow parts. An eval that uses only Chamfer/IoU will miss thin walls and cavities; use explicit requirement checks (wall thickness, volume, hole count).

### 1.5 CAD-Assistant (Mallis et al., ICCV 2025) — agentic, tool-augmented
- URL: https://arxiv.org/abs/2412.13810 (HTML: https://arxiv.org/html/2412.13810), code: https://github.com/dimitrismallis/CAD-Assistant
- Task: generic CAD agent: CAD question answering (SGPBench), sketch autoconstraining (SketchGraphs), hand-drawn sketch parameterization; qualitative demos of revolve and fillet.
- Representation: Python actions on the FreeCAD API inside a live FreeCAD project; the agent sees the evolving CAD state after each step (Sec. 3.1). Actions as code, not natural-language tool calls (Sec. 3.2).
- Feedback: multimodal. Tools (Table 1): "Sketch Recognizer" and "Solid Recognizer" render the model and plot parameters; "Constraint Checker" analyzes constraints; cross-section image extractor; interpreter output. Loop runs until the planner emits TERMINATE.
- Key finding on representation (Table 2, GPT-4o, 2D SGPBench): JSON with point-based parameters 0.748 accuracy vs the original implicit SGPBench format 0.674, DXF 0.671; precise rendering 0.754 vs hand-drawn sketch image 0.616. So structured, explicit-coordinate JSON is about as good as a clean render, and both beat implicit parameterization.
- Headline results (Table 3): GPT-4o planner, SGPBench 2D accuracy 0.686 -> 0.791, 3D 0.782 -> 0.857 with CAD-Assistant tools. Small model (GPT-4 mini) gains little: tools need a strong planner. Table 5: both the multimodal recognizer and the constraint checker add to autoconstraining gains; 5-shot demos help further.
- Failure modes (Sec. 4.3, 100 failed CQA trajectories reviewed): most failures are wrong VLM reasoning or misreading of tool renders (for example "confusing a trapezoid with a triangle"). Tool-use validity was 98.5% over 200 trajectories; remaining errors are wrong FreeCAD API use. Intro also lists: VLMs fail to orient primitives and place them, and cannot predict the effects of fillet/chamfer/constraints (Sec. 1).
- Note for coalesce: this is the closest prior work to coalesce's design (agent + live CAD state + renders + structured parameter dumps + checkers). Its JSON-vs-render result is direct evidence for structured queries.

### 1.6 Query2CAD (Badagabettu, Yarlagadda, Barati Farimani, CMU, 2024)
- URL: https://arxiv.org/abs/2406.00144 (HTML: https://arxiv.org/html/2406.00144), code: github.com/akshay140601/Query2CAD
- Task: natural-language query -> FreeCAD Python macro.
- Representation: FreeCAD macro code.
- Feedback: isometric render -> BLIP2 caption -> LLM refiner; VQA score threshold decides whether to refine; up to 3 loops. Human-in-the-loop captions to correct BLIP2 false negatives. Error refinement for code that does not run.
- Dataset: 57 hand-made queries (21 easy, 20 medium, 16 hard) (Sec. 5.1). Success is strict: "exact 3D CAD model as specified", any deviation = failure (Sec. 5.2).
- Headline results: GPT-4 Turbo 53.6% first try, +23.1 points after refinement (abstract); per level easy 95.23%, medium 70%, hard 41% (Sec. 5.2). Most gain comes from the first refinement (Sec. 6).
- Failure modes (Sec. 6, Fig. 5): of 13 GPT-4 Turbo failures, 69% were code that never ran even after error refinement, 31% wrong structure. Medium questions test spatial arrangement ("cube on top of a sphere"). BLIP2 captions alone did not drive good refinement; human feedback that states what is wrong and how to fix it "made refinement much better" (Sec. 6, 7).
- Note for coalesce (inference): non-executing code was the main failure. A feature-graph API with typed operations and actionable errors directly attacks this. The human-feedback finding supports actionable, specific error and requirement messages over generic captions.

### 1.7 CAD-LLM (Wu et al., Autodesk AI Lab / U. Montreal, NeurIPS 2023 workshop)
- URL: https://www.research.autodesk.com/publications/ai-lab-cad-llm/ (PDF: https://www.research.autodesk.com/app/uploads/2024/05/cadllm_neurips2023_workshop-1.pdf; workshop listing: https://neuripscreativityworkshop.github.io/2023/papers/ml4cd2023_paper15.pdf)
- Task: fine-tune a pre-trained language model to complete or generate parametric 2D engineering sketches (prefix of 20-80% of a sketch -> full sketch), per the project page figure caption.
- Representation: sketch entities (and constraints) serialized as text. Follow-up: CadVLM adds vision (https://arxiv.org/abs/2409.17457).
- Feedback: none.
- Metrics: entity accuracy, sketch accuracy, CAD F1 (according to the search-engine summary of the PDF; I could not open the PDF, so this is unverified).
- Headline results and failure modes: not verified (PDF could not be fetched by my tool). Project page only says LMs "can be fine-tuned on engineering sketches and achieve remarkable performance".

### 1.8 OpenECAD (Yuan, Shi, Huang, ECNU, 2024; Computers & Graphics)
- URL: https://arxiv.org/abs/2406.09913 (HTML: https://arxiv.org/html/2406.09913), journal: https://www.sciencedirect.com/science/article/pii/S0097849324001833
- Task: image(s) of a 3D part -> structured 2D sketch + 3D construction code usable with CAD APIs.
- Representation: own Python-like code format for sketch-extrude sequences, with sketches defined through "dependency relationships" (references) (abstract, Sec. 4.2). Fine-tuned small VLMs (0.55B-3.1B).
- Feedback: none (refinement listed only as future work, Sec. 6.6.1).
- Metrics: curves accuracy, loops accuracy, loops score, execution score, overall score (Sec. 6.1, Table 5).
- Headline results (Table 5, default view): OpenECAD 2.4B overall 88.80 vs gpt-4o-mini 39.72; on designs with more than one sketch-extrude pair the 2.4B drops to 75.76. Four views (iso + 3 orthographic) performed worse than one view; a single "transparent" view (hidden edges visible) helped multi-step designs (Sec. 6.3).
- Failure modes (Sec. 6.4.2, Table 6) — the most detailed list found:
  - complex or irregular sketches: wrong or self-intersecting loops -> broken extrusions;
  - "lacks spatial reasoning": wrong size ratios between sketch and extrusion (too long/short, too big/small);
  - poor numeric understanding;
  - in multi-step models, later sketches are placed at the wrong position (references/positions wrong);
  - cannot tell a solid cylinder from a hollow one from the render; holes and concave vs convex confused.
- Note for coalesce (inference): "transparent view helps, more views hurt" suggests coalesce's rendered views should be few, labeled, and show hidden edges or section cuts, not many unlabeled images. Solid-vs-hollow confusion is a case where a structured query (volume, face count, through-hole list) beats an image.

### 1.9 FlexCAD (Zhang, Sun et al., Zhejiang U. / Microsoft Research Asia, ICLR 2025)
- URL: https://arxiv.org/abs/2411.05823 (HTML: https://arxiv.org/html/2411.05823), code: https://github.com/microsoft/FlexCAD
- Task: controllable generation/editing: mask one part of an existing CAD model at a chosen level (sketch-extrusion, extrusion, sketch, face, loop, curve) and generate a replacement. This is a local-edit task, close to coalesce's "edit a feature graph".
- Representation: CAD model as structured text (hierarchy of sketch-extrude, face, loop, curve tokens); fine-tuned Llama-3-8B with hierarchy-aware masking.
- Feedback: none.
- Metrics: COV, MMD, JSD (distribution metrics), novelty, uniqueness, prediction validity (PV), human-rated realism (Sec. 4.1-4.2).
- Headline results (Table 1, DeepCAD test, sketch-level): FlexCAD PV 93.4% vs GPT-4o (5-shot) 62.3%, realism 39.6% vs 23.2%. Extrusion-level PV 93.3% vs GPT-4o 48.8%.
- Failure modes (App. A.6): hallucinated "plausible CAD texts" that produce irregular polygons when asked to replace a loop with 5-8 lines; 4-line replacements stay regular.
- Note for coalesce (inference): GPT-4o few-shot produced valid local edits only about half the time on raw sequence text, which suggests raw low-level tokens are a poor edit surface for general LLMs; a named feature graph with typed parameters should be easier.

### 1.10 BlenderLLM + BlendNet + CADBench (Du et al., CUHK-Shenzhen, 2024)
- URL: https://arxiv.org/abs/2412.14203 (HTML: https://arxiv.org/html/2412.14203), code/data: https://github.com/FreedomIntelligence/BlenderLLM, benchmark: https://huggingface.co/datasets/FreedomIntelligence/CADBench
- Task: natural-language instruction -> Blender Python (bpy) script that builds an object (more "3D object" than mechanical part).
- Representation: bpy code. Fine-tuned Qwen2.5-Coder-7B-Instruct (Sec. 5.1).
- Feedback: used for training (self-improvement: generate, render, filter with a "cascade filter", retrain), not at inference (Sec. 3.2).
- Benchmark CADBench: 700 instructions; CADBench-Sim (synthetic) and CADBench-Wild (internet-sourced). No ground-truth geometry: each sample has per-sample criteria (3 major dimensions — object attributes, spatial understanding/structure, instruction understanding — and 8 minor ones), drafted by GPT-4o and then manually verified; GPT-4o judges renders against the criteria (Sec. 4.3-4.4).
- Metrics: criteria pass scores per dimension (Attr., Spat., Inst.) and syntax error rate E_syntax (Table 2).
- Headline results (Table 2): BlenderLLM iteration 3 average 0.748 (Sim) / 0.664 (Wild), syntax error 3.4% / 3.5%. Baselines: Gemini-1.5-Pro 0.468 / 0.380 with 30.2% / 38.0% syntax errors; Qwen2.5-Coder-7B-Instruct 0.353 / 0.310. Self-improvement peaks at iteration 3; iteration 4 drops on Wild (0.608).
- Failure modes: syntax/execution errors dominate weak models (e.g. Mistral-7B 96.8% syntax error). In all rows the instruction-following score is lowest, then spatial, then attributes (from Table 2; inference about the ordering is mine). Limitations: no multi-turn interaction, basic modeling only (Limitation section).
- Note for coalesce: CADBench's "per-sample criteria instead of ground truth" is the nearest published analogue of requirements-as-tests, but its checks are judged by a VLM from images, not computed from geometry.

### 1.11 CADSmith (Barkley, Loghmani, Barati Farimani, CMU, March 2026) — agentic, geometric + visual feedback
- URL: https://arxiv.org/abs/2603.26512 (HTML: https://arxiv.org/html/2603.26512), code: https://github.com/jabarkle/CADSmith
- Task: text -> CadQuery via 5 agents (planner, coder with RAG over CadQuery docs, executor, validator judge, refiner). Inner loop fixes execution errors; outer loop fixes geometry.
- Representation: CadQuery.
- Feedback: the judge (Claude Opus; coder is Claude Sonnet, to avoid self-evaluation bias) gets prompt + code + exact OpenCASCADE kernel metrics (volume, bounding box, face/edge/vertex counts, center of mass, solid validity) + one 3-view render (isometric; high-angle rear for top-face holes/bores; front profile for heights) (Sec. III-E).
- Benchmark: 100 hand-written prompt + reference CadQuery pairs: T1 primitives (50), T2 engineering parts with booleans, hole patterns, counterbores, gears (25), T3 lofts, sweeps, shells, revolves, multi-body (25). Hand-written because LLM-generated datasets had references that "silently diverged from prompt intent through wrong dimensions, misplaced features, or simplified geometry". Prompts use explicit mm, Z-up, origin-centered, coordinates for holes (Sec. III-A).
- Metrics: execution rate, Chamfer distance (median and mean), F1, IoU, in absolute mm after ICP alignment (Sec. IV).
- Headline results (Table I): zero-shot exec 95%, mean CD 28.37, median IoU 0.8085; no-vision pipeline exec 99%, mean CD 18.19, IoU 0.9563; full (vision) exec 100%, mean CD 0.74, IoU 0.9629. 88/100 entries passed on iteration 0; mean 0.13 refinement iterations.
- Vision vs geometry (Sec. IV-B, V): on T1/T2 the no-vision variant is "comparable" (kernel metrics suffice); on T3 removing the render raised mean CD 1.42 -> 49.68 and dropped mean F1 0.85 -> 0.74. Reason given: "false convergence" — plausible volume and bounding box but wrong structure.
- Failure modes (Sec. IV-D): (1) valid but structurally different construction from the reference (T3 F1 about 0.57-0.59); (2) "near-miss": quadcopter frame with small gaps between arms and hub passed both kernel metrics and the vision judge (F1 0.963, IoU 0.985) but is not manufacturable.
- Note for coalesce (inference): this is the strongest direct evidence for combining both channels. Kernel aggregates (bbox, volume) are too coarse to catch structure errors; images are too coarse to catch small gaps. Topology checks (single solid, body count, face adjacency/contact between named features) would catch the quadcopter gap; that is a requirement-as-test, not a render.

### 1.12 CADFusion — "Text-to-CAD Generation Through Infusing Visual Feedback in LLMs" (Wang, Yuan, Sun, Bian, MSRA, ICML 2025)
- URL: https://arxiv.org/abs/2501.19054 (HTML: https://arxiv.org/html/2501.19054), code: https://github.com/microsoft/CADFusion, PMLR: https://proceedings.mlr.press/v267/wang25eg.html
- Task: text -> CAD parametric sequence (sketch-extrude, string format).
- Representation: sequence string (App. B.1); LLM backbone.
- Feedback: visual feedback used at training time: render generated sequences, have a large vision model (LVM) score them, then DPO toward preferred renders; alternate with supervised sequence learning (abstract, Sec. 3.3). Motivation: rendering is many-to-one, so sequence supervision alone is not enough.
- Metrics: invalidity ratio, LVM score, average human rank, plus geometric metrics (Table 1; exact numbers not extracted here).
- Headline results (Sec. 4.2): beats GPT-4o on all metrics; comparable or better than Text2CAD, with largest gains on visual metrics. GPT-4o "frequently fails to produce renderable results" (high IR). Text2CAD gives oversimplified shapes (multiple cubes/panels).
- Failure modes (App. D.8, Sec. 5): invalid when the instruction has too many elements (e.g. more than 20 loops and 50 curves); discrepancy when many distinct items; cannot build letters/words. LVMs degrade with multiple images, so they used only one view (Sec. 5).

### 1.13 LLMForge — "Foundation Models for Automatic CAD Generation" (de Curtò, Guillén, de Zarzà, July 2026, book chapter)
- URL: https://arxiv.org/abs/2607.05573 (HTML: https://arxiv.org/html/2607.05573)
- Task: text spec -> JSON-schema parametric part -> mesh; 97 problems in 4 families: plates with holes and bolt circles, multi-feature boxes, flanged cylinders, L-brackets.
- Representation: JSON parameters under a schema (not general code).
- Feedback: two regimes. IterTracer: ray-traced render + analytic image metrics (silhouette IoU, hole visibility, edge clearance, aspect ratio). IterVision: Qwen2.5-VL-72B critic on renders with chain-of-thought.
- Results (abstract, Sec. 6): under IterTracer the top 4 of 7 models cluster at 98.97% mesh success; IterVision gives 100% watertight meshes for the leading model. Analytic feedback saturates at round 1 and regresses afterwards; VLM feedback keeps improving through rounds 2-3. Authors propose analytic critique first, VLM only for ambiguous parts.
- Failure modes (Sec. 6): malformed parameters that schema validation does not catch (zero-size dimensions, inverted hole coordinates); one model got high VLM scores while its geometry nearly always failed — the VLM judged renders "plausible" when topology was invalid; cylinders score lower because they lack strong visual cues and the VLM rates them conservatively.
- Note (inference): the "VLM praises invalid geometry" result is a warning against using a VLM judge as the eval oracle. Semantic validation (non-zero sizes, holes inside the face) belongs in the tool's error layer.

### 1.14 CADReasoner (Kabisov et al., MSU / FusionBrain, 2026)
- URL: https://arxiv.org/abs/2603.29847 (HTML: https://arxiv.org/html/2603.29847)
- Task: scan/mesh -> CadQuery reverse engineering, by iterative self-editing of the program.
- Representation: CadQuery.
- Feedback: geometric/visual diff: the model sees the target (multi-view overlays + point cloud) and the render of its current program, then outputs an edited program; best-so-far chosen by Chamfer distance to the scan (abstract, Sec. 3.6).
- Metrics: CD, IoU, IR on DeepCAD, Fusion 360, MCB; clean and simulated-scan tracks.
- Results (Sec. 4.3, qualitative from text; I did not extract table numbers): beats supervised baselines at step 1 on DeepCAD/Fusion 360 and improves further over 5 steps; on the harder MCB set step 1 trails, but 5 iterations give large gains. Point-cloud-only branch is on par at step 1 and exceeds cadrille-SFT after self-editing.
- Failure modes: not reported as a section (not found). Abstract says frozen-VLM agents are limited by "weak 3D grounding".
- Note (inference): evidence that iterative edit loops help most on complex parts, consistent with CADSmith T3.

### 1.15 "How Can Large Language Models Help Humans in Design and Manufacturing?" (Makatura et al., MIT, 2023)
- URL: https://arxiv.org/abs/2307.14377 (HTML: https://arxiv.org/html/2307.14377)
- Task: broad qualitative study of GPT-4 across design, design spaces, DfM, performance, inverse design. CAD part: CSG (own primitives, PyVista, OpenJSCAD), sketch-based CAD in Onshape via a small DSL (createSketch, extrude, circle, rectangle), URDF.
- Feedback: human-in-the-loop prompting (no automated loop).
- Metrics: none quantitative (case studies).
- Failure modes (their summary list): "GPT-4 struggles with spatial reasoning, analytical reasoning, and computations" (L.1); inaccurate results/justifications (L.2); "struggles to respect multiple requests concurrently" (L.3); "forgets/introduces errors when modifying previously-generated designs" (L.4, iterative editing).
- Specific sketch-CAD failures (Sec. 4.1.5): GPT-4 "performs poorly" writing raw FeatureScript, so they used a DSL. When a sketch is placed on a face (e.g. top of a leg), the sketch origin moves to that face's center and GPT-4 "will often ignore this or won't be able to account for it when pointed out". It cannot rotate sketch planes: car wheels were extruded vertically from the ground plane. Allowing global coordinates plus a plane only for extrude direction fixed placement.
- Note for coalesce (inference): local sketch coordinate frames and face references are a top failure source. Coalesce should (a) let the agent state positions in world coordinates or in clearly reported local frames, (b) report each sketch plane's origin and axes in query results, and (c) label axes in rendered views.

### 1.16 CADTests / CADTestBench — "Text-to-CAD Evaluation with CADTests" (May 2026) — most relevant eval paper for coalesce
- URL: https://arxiv.org/abs/2605.07807 (HTML: https://arxiv.org/html/2605.07807)
- Task: evaluation method. Each prompt gets a suite of executable Python tests that query the generated B-Rep with CadQuery (selectors, topology counts, bbox, areas, volumes, center of mass, containment, line-geometry intersections) (Sec. 1).
- Why: Chamfer/IoU against one reference is wrong for text-to-CAD because prompts leave degrees of freedom open; tests are reference-free, explicit, interpretable (failing tests print why), deterministic and cheap (Sec. 1-2).
- Construction: built on CADPrompt's 200 parts, separate suites for abstract and detailed prompts; 5,937 tests, about 15 per sample. Tests drafted by Claude-4.6-Sonnet, then refined for 4 rounds with mutation analysis: tests must pass on the reference (and 5 augmented copies: small scale, translate, 90-degree rotations; no scaling for detailed prompts) and each LLM-made "mutant" (wrong dimension, removed feature, changed topology) must fail at least one test (Sec. 5). After refinement: soundness 0.982 / 0.972, mutation score 0.91 / 0.93 (detailed / abstract) (Table 2). Without refinement, best planners catch only about 65% of mutants (Table 1).
- Test categories: solid/shell validity, topology, geometry type, dimensions and ratios, volume, spatial relations (alignment, symmetry, offsets) (Sec. 5).
- Metrics: Pass-Rate (all tests pass), Requirement Score (fraction of requirement groups fully passed), per-category accuracy, Invalid-Ratio; invalid outputs count as failures (Sec. 5).
- Headline results (Table 3, detailed / abstract PR): Text2CAD 0.025 / 0.085; CADCodeVerify (GPT-4.1) 0.410 / 0.630; ReAct (execution-error loop) GPT-5.2 0.480 / 0.695; ReAct + 4-view images GPT-5.2 0.485 / 0.695 (no gain); ReAct Claude-4.6-Sonnet 0.580 / 0.715; CADTests-in-the-loop + log queries Claude-4.6-Sonnet 0.625 / 0.810 (best).
- Findings (Sec. 6): (iv) kernel execution feedback cuts invalidity sharply; (v) "Visual feedback via multi-view renderings does not improve performance" and costs many tokens; (vi) letting the agent write and run its own tests, plus log queries of intermediate geometry, is the best feedback. Easiest categories: solid validity and geometry type; hardest: topological, dimensional, volumetric, spatial. Fine-tuned DeepCAD-style models fail to generalize. Human study: test-based evaluation agrees with expert judgment better than other metrics (Table 4; inter-annotator Cohen's kappa 0.54). Example: tests caught holes that broke through a disk edge as open slots (Fig. 4).
- Note for coalesce: this is almost exactly "requirements-as-tests". Reuse the method (mutation analysis to validate the test suite) for coalesce's eval set.

### 1.17 Other 2026 benchmarks (abstract-level only)
- Text2CAD-Bench (May 2026): 600 human-curated examples in 4 levels (L1-L2 basic geometry and standard features; L3 complex topology and freeform surfaces; L4 non-mechanical domains); each has a non-expert geometric prompt and an expert procedural prompt. Models "perform reasonably on basic geometry but degrade substantially on complex topology and advanced features." https://arxiv.org/abs/2605.18430
- MUSE (May 2026): multi-component B-Rep assembly benchmark with structured "Design Specifications"; scores executability, geometric validity, structure and interface fidelity (component correspondence, connectivity, interface types, clearances via a reference "Physical Assembly Graph"), and functional consistency. "High execution success does not necessarily translate into correct assembly structure or interface geometry." https://arxiv.org/abs/2605.28579
- Text-to-CadQuery (May 2025): adds 170K CadQuery annotations to Text2CAD data; fine-tunes 6 open LLMs; best top-1 exact match 69.3% (from 58.8%), Chamfer distance down 48.6%; bigger models do better. https://arxiv.org/abs/2505.06507
- 3D-PreMise (Jan 2024): dataset of industrial parametric shapes; GPT-4 writes 3D-software code, then self-corrects from a rendered image. https://arxiv.org/abs/2401.06437 (CADCodeVerify reports that this image-only refinement lowered GPT-4's compile rate on hard prompts by 20 points; see 1.1.)

### 1.18 Other related work (abstract-level)
- CAD-Editor (Yuan, Sun, Liu, Bian, MSRA, ICML 2025): first text-based CAD editing framework; "locate-then-infill" (find region to change, then infill); training triplets synthesized by design-variation models + LVLM-written edit instructions. Representation: DeepCAD-style sequence text. https://arxiv.org/abs/2502.03997 . Note (inference): "locate" is the step coalesce semantic selectors make explicit.
- SGP-Bench (Qiu et al., 2024): tests whether LLMs can answer semantic questions about SVG and CAD programs without seeing renders; stronger reasoners do better; symbolic instruction tuning helps. https://arxiv.org/abs/2408.08313 . Used by CAD-Recode and CAD-Assistant as a CAD-QA benchmark.
- cadrille (2025): one VLM for point cloud + image + text -> CAD code; SFT on procedural data, then online RL (GRPO) with programmatic feedback beats offline alternatives. https://arxiv.org/abs/2505.22914 . CADTestBench reports cadrille gave degenerate solutions on its prompts (https://arxiv.org/html/2605.07807, Sec. 6).
- Zoo (KittyCAD) Text-to-CAD and KCL: company-reported KCL-generation error rate cut "from 50% to 16%" (Aug 2025) — not an independent benchmark. https://docs.zoo.dev/blog/whats-new-august . Zookeeper agent (Feb 2026): edits KCL, is aware of user-selected faces/edges, and "by default ... will trigger snapshots to review and validate any modifications" (render-based self-check). https://zoo.dev/blog/announcing-zookeeper . No published eval numbers found.

- Seek-CAD (2025): training-free, local DeepSeek-R1; renders step-wise perspective images; a VLM plus R1's chain-of-thought give feedback for the next round; new "SSR" (sketch, sketch-based feature, refinements) dataset with wider command coverage. https://arxiv.org/abs/2505.17702 (results not extracted).
- EvoCAD (2025): population of CAD programs improved by evolutionary selection/mutation/crossover with VLM and reasoning-model judges; evaluated on CADPrompt; adds Euler-characteristic topology metrics because spatial metrics miss topology. https://arxiv.org/abs/2510.11631 (results not extracted).

## 2. Datasets

| Dataset | What it is | Size | Representation | Notes for coalesce | URL |
|---|---|---|---|---|---|
| ABC | CAD models from public Onshape documents, parametric curves and surfaces | about 1M models | B-Rep (STEP), meshes, FeatureScript | Has duplicates and invalid shapes (as noted by CAD-Coder, https://arxiv.org/html/2505.14646 Sec. 2.3). No text or requirements. | https://arxiv.org/abs/1812.06216 |
| DeepCAD | Sketch-extrude construction sequences parsed from Onshape public docs linked from ABC | 178,238 models | JSON command sequences (line, arc, circle, extrude) | Base of Text2CAD, GenCAD-Code, CAD-Coder, CADFusion, FlexCAD, Text-to-CadQuery. Random 90/5/5 split. Biased to rectangles and cylinders (Text2CAD Sec. 6). Heavily used in training data of fine-tuned models, so high contamination risk. | https://arxiv.org/abs/2105.09492 , parsing note: https://github.com/ChrisWu1997/DeepCAD |
| Fusion 360 Gallery (reconstruction subset) | Human design sequences in a sketch+extrude language + "Fusion 360 Gym" environment | 8,625 sequences | Sketch-extrude program | License: non-commercial research only; no redistribution of the full set (https://github.com/AutodeskAILab/Fusion360GalleryDataset/blob/master/LICENSE.md). Human-made, so more realistic than DeepCAD. | https://arxiv.org/abs/2010.02392 |
| SketchGraphs | 2D sketches as constraint graphs from real Onshape models | 15M sketches | primitives + constraints | Useful only for sketch-level tasks. | https://arxiv.org/abs/2007.08506 |
| Text2CAD data | DeepCAD models with 4-level text prompts made by Mistral + LLaVA-NeXT | about 170K models, 660K prompts | text + DeepCAD sequence | Machine-written prompts, noisy (Text2CAD Sec. 15). Text-to-CadQuery adds 170K CadQuery scripts (https://arxiv.org/abs/2505.06507); CAD-Coder (text) adds 110K (https://arxiv.org/abs/2505.19713). | https://arxiv.org/abs/2409.17106 |
| CADPrompt | 200 prompts (abstract and detailed) + expert CadQuery + meshes | 200 | text + CadQuery + STL | Public on GitHub since 2024, so possibly in model training data (inference). Extended by CADTestBench with about 15 tests per sample. | https://arxiv.org/abs/2410.05340 , https://github.com/Kamel773/CAD_Code_Generation |
| CADTestBench | CADPrompt + executable CadQuery tests, mutation-validated | 200 parts x 2 prompt styles, 5,937 tests | text + tests | Closest public match to coalesce's requirement format. | https://arxiv.org/abs/2605.07807 |
| CADSmith benchmark | 100 hand-written prompts + reference CadQuery in 3 tiers | 100 | text + CadQuery | Good tier design (T2 engineering parts with booleans and hole patterns). | https://arxiv.org/abs/2603.26512 |
| Text2CAD-Bench | 600 human-curated prompts, 4 levels, dual prompt styles | 600 | text + CAD | Includes complex topology and freeform. | https://arxiv.org/abs/2605.18430 |
| MUSE | Multi-component assemblies with structured design specs | not extracted | B-Rep assemblies | Interface/clearance checks; relevant if coalesce adds assemblies. | https://arxiv.org/abs/2605.28579 |
| CADBench | 700 instructions with per-sample criteria, VLM-judged | 700 | text + criteria | Criteria are checked from images, not geometry. | https://arxiv.org/abs/2412.14203 |
| Query2CAD set | 57 queries, easy/medium/hard | 57 | text | Small, strict success definition. | https://arxiv.org/abs/2406.00144 |
| NIST MBE PMI test models (FTC, CTC, STC) | Real-style machined parts with full GD&T/PMI, STEP AP203/AP242 + native CAD | about 16 models (FTC 06-11, CTC 01-05, plus STC) | STEP with semantic PMI | NIST: "can be used without any restrictions" (acknowledgement requested). Tolerances and datums give ready-made requirements. FTC 07-10 form an assembly. | https://www.nist.gov/ctl/smart-connected-systems-division/smart-connected-manufacturing-systems-group/mbe-pmi-0 |

Contamination evidence: a search summary states that an ECCV 2024 supplement found about 14% of the DeepCAD test set has geometric near-duplicates in the training set (https://www.ecva.net/papers/eccv_2024/papers_ECCV/papers/07791-supp.pdf). I could not open the PDF, so this number is unverified.


## 3. Synthesis 1: common failure modes and which coalesce features address them

Rule for this section: the "evidence" column is from the papers; the "coalesce feature" column is my inference (design proposal), not a paper result.

| # | Failure mode | Evidence | Coalesce feature that addresses it (inference) |
|---|---|---|---|
| F1 | Output does not run / invalid or degenerate geometry | Query2CAD: 69% of GPT-4 Turbo failures were code that never ran (https://arxiv.org/html/2406.00144 Sec. 6). CADPrompt compile rates 73.5-96.5% (https://arxiv.org/html/2410.05340v2 Sec. 6). CADBench baselines 25-99% syntax error (https://arxiv.org/html/2412.14203 Table 2). Degenerate params: zero extrusion, same start/end point (Text2CAD Sec. 15); zero-size dims, inverted hole coords pass schema checks (LLMForge, https://arxiv.org/html/2607.05573 Sec. 6). Execution-error feedback (ReAct) drops invalidity to about 0 (CADTests Table 3, https://arxiv.org/html/2605.07807). | Typed feature-graph operations instead of free code; validate each edit at once (non-zero sizes, profile closed, hole inside face). **Actionable errors**: name the feature and parameter, say what is wrong, give the allowed range and one suggested fix. Keep the last good state so one bad edit does not destroy the model. |
| F2 | Spatial placement: wrong position, wrong orientation, local vs global frames | GPT-4 ignores the moved origin when it sketches on a face, and cannot rotate sketch planes (Makatura, https://arxiv.org/html/2307.14377 Sec. 4.1.5). Later sketches in multi-step models placed wrong (OpenECAD, https://arxiv.org/html/2406.09913 Sec. 6.4.2). Sub-part dislocation/offsets (CAD-Coder text, https://arxiv.org/html/2505.19713 App. F). Positional feedback is a main feedback type (CADCodeVerify App. C.1). VLMs fail to "orient primitives and generate accurate placements" (CAD-Assistant, https://arxiv.org/html/2412.13810 Sec. 1). Spatial-relation tests are among the hardest categories (CADTests Sec. 6). | **Semantic selectors** for placement ("top face of base", "axis of boss") so the agent does not compute frames. **Structured queries** that return each sketch plane's origin, normal and in-plane axes in world coordinates. **Labeled views** with a world axis triad and named faces. Requirements of type "relation" (concentric, centered, offset, symmetric). |
| F3 | Structural configuration wrong (right size, wrong shape/feature arrangement) | Largest error class, 48% (CADCodeVerify App. C.2, https://arxiv.org/html/2410.05340v2). "False convergence": plausible bbox and volume but wrong structure (CADSmith Sec. V, https://arxiv.org/html/2603.26512). Hallucinated irregular polygons (FlexCAD App. A.6). Oversimplified shapes (CADFusion Sec. 4.2 on Text2CAD). | Feature-graph summary query (features, bodies, face counts per feature, what each feature touched). Labeled views as a coarse check. Requirements that count and type features (n through-holes of diameter d, one revolve, shell open at top). |
| F4 | Wrong dimensions, proportions, numbers | Wrong size ratios between sketch and extrusion; poor numeric understanding (OpenECAD Sec. 6.4.2). Wrong dimensions from images (CAD-Coder image, https://arxiv.org/html/2505.14646 Sec. 5). Spatial-precision errors (CADCodeVerify App. C.2). Dimensional and volumetric tests are hard (CADTests Sec. 6). Images "cannot resolve dimensional errors" (CADSmith abstract). | Named parameters (expressions, not baked numbers). **Structured measurement queries** (distance, bbox, diameter, wall thickness) with exact numbers. **Requirements-as-tests** with explicit tolerances and the measured value in the failure message. |
| F5 | Boolean and operation-type errors; hollow vs solid; holes that break out; gaps between parts | Extrude vs cut confused (CAD-Coder text App. F). Solid vs hollow cylinder confused from images (OpenECAD Sec. 6.4.2). Holes that break through a disk edge as open slots (CADTests Fig. 4). Small gaps between arms and hub passed both kernel metrics and VLM judge (CADSmith Sec. IV-D). Gaps between desk legs and top missed by distance metrics (CADCodeVerify Sec. 7). Chamfer reward hacking on thin walls and cavities (CAD-Coder text App. F). | Every boolean reports its effect: bodies before/after, volume change, "no-op" warning if nothing changed, warning if the result splits into several solids. Queries: body count, hole list (through/blind, enclosed or breaking an edge), minimum wall thickness, contact/gap between named features. Default requirements on every task: valid single solid unless stated. |
| F6 | References and edits: regressions when modifying an existing design | GPT-4 "forgets/introduces errors when modifying previously-generated designs" (Makatura L.4). Editing needs a "locate" step (CAD-Editor, https://arxiv.org/abs/2502.03997). GPT-4o few-shot local edits valid only 48.8-62.3% of the time on raw sequence text (FlexCAD Table 1, https://arxiv.org/html/2411.05823). | **Semantic selectors** that survive upstream edits (stable names, not face indices). An edit diff after each change (which features and faces changed). Requirements stay attached to the model and are re-run after each edit, so regressions show at once. |
| F7 | Complexity scaling: multi-step parts, many elements | OpenECAD drops from 88.8 to 75.8 overall on multi-step designs (Table 5). CADFusion fails with more than 20 loops / 50 curves (App. D.8). CADSmith T3 F1 0.886 vs about 0.999 on T1/T2 (Table II). Text2CAD-Bench: large drop on complex topology (https://arxiv.org/abs/2605.18430). Iterative editing helps most on complex sets (CADReasoner on MCB, https://arxiv.org/html/2603.29847 Sec. 4.3). | Incremental, feature-by-feature workflow with a check after each feature; "log queries" of intermediate state (CADTests+Log was the best baseline, Table 3). |
| F8 | Misreading images | CAD-Assistant failures mostly wrong reasoning or misread renders, e.g. trapezoid vs triangle (Sec. 4.3). VLM gave high scores to near-always-invalid geometry (LLMForge Sec. 6). More images can hurt: 4 views worse than 1 (OpenECAD Sec. 6.3); LVMs degrade with multiple images (CADFusion Sec. 5). Hand-drawn image 0.616 vs precise render 0.754 vs JSON 0.748 (CAD-Assistant Table 2). | Few, purpose-chosen **labeled views** (iso + one view that shows holes + section or hidden-edge view), with dimensions or feature labels overlaid. Always pair an image with the structured numbers. Never use a VLM verdict as the pass/fail oracle in the eval. |
| F9 | Ambiguous prompts and wrong references in datasets | Prompt wording changes results (CADCodeVerify Sec. 7). Machine-written prompts are noisy (Text2CAD Sec. 15). LLM-made references "silently diverged from prompt intent" (CADSmith Sec. III-A). Single-reference geometry metrics penalize valid alternatives (CADTests Sec. 2). | Eval tasks state requirements explicitly; success is judged by tests, not by similarity to one reference. |

## 4. Synthesis 2: does visual feedback or geometric feedback help more?

Evidence that precise geometric / execution feedback helps more:
- CADCodeVerify: a geometric-solver baseline (exact property differences, but it uses the ground truth) beats their visual method on point-cloud distance, 0.102 vs 0.132 (GPT-4 zero-shot, Table 2, https://arxiv.org/html/2410.05340v2). The authors call it an upper bound.
- CADTests: adding 4-view renders to an execution-feedback loop gave no gain (GPT-5.2 pass rate 0.480 -> 0.485 detailed, 0.695 -> 0.695 abstract); test-based feedback with log queries was best (Claude-4.6-Sonnet 0.625 / 0.810) (Table 3, Sec. 6, https://arxiv.org/html/2605.07807).
- CADSmith: on T1 primitives and T2 engineering parts, kernel metrics alone were about as good as kernel + vision (Sec. IV-B, https://arxiv.org/html/2603.26512).
- 3D-Premise-style image-only refinement reduced GPT-4 compile rate on hard prompts by 20 points (reported by CADCodeVerify Sec. 6).
- Query2CAD: generic image captions (BLIP2) did not drive good refinement; specific "what is wrong and how to fix it" feedback did (Sec. 6, https://arxiv.org/html/2406.00144).
- LLMForge: a VLM critic rated near-always-invalid geometry as good (Sec. 6, https://arxiv.org/html/2607.05573).
- Training-time: in CAD-Coder (text), the Chamfer-distance reward (GRPO) gave the largest gain (mean CD 74.55 -> 17.34 without CoT) (https://arxiv.org/html/2505.19713 Sec. 5.2).

Evidence that visual feedback helps:
- CADSmith T3 (lofts, sweeps, shells, multi-body): removing the render from the judge raised mean Chamfer distance 1.42 -> 49.68 and lowered mean F1 0.85 -> 0.74. Reason: kernel metrics were only aggregates (bbox, volume, counts), which allow "false convergence" (Sec. IV-B, V).
- CADCodeVerify: visual question-answer feedback beats no feedback and beats 3D-Premise (Table 2); removing reference images hurts (Sec. 6).
- LLMForge: VLM feedback kept improving through rounds 2-3, while analytic feedback saturated at round 1 (Sec. 6).
- CADFusion: visual preference feedback at training time improves visual-quality metrics over sequence-only training (Sec. 4.2, https://arxiv.org/html/2501.19054).
- CAD-Assistant: a precise render (0.754) is about as good as the best structured text (JSON 0.748) for sketch QA, and both beat implicit parameterization (0.674) (Table 2, https://arxiv.org/html/2412.13810).

Conclusion (inference):
- Exact, targeted geometric checks are the stronger signal for validity, dimensions, counts and positions. When the checks are rich and specific to the requirements (CADTests), images add nothing measurable. When the checks are coarse aggregates (CADSmith's bbox/volume), images catch structure errors the numbers miss.
- So images are a backstop for "unknown unknowns", not the main signal. The better the structured queries and requirement tests, the less the images add.
- Neither channel caught every defect: CADSmith's small gaps passed both kernel metrics and the VLM. Topology and contact checks (body count, face adjacency, gap between named features) are needed for that class.
- For coalesce: make structured queries and requirement checks the primary loop; keep a small set of labeled views as a cheap secondary check; measure the value of each with ablations in the eval (see 5.6).

## 5. Synthesis 3: proposal for the coalesce eval set (all of this section is proposal / inference unless cited)

### 5.1 Task format
One directory per task:
- `task.yaml`: id, family, tier (T1 simple, T2 engineering, T3 multi-feature, following CADSmith tiers https://arxiv.org/html/2603.26512 Sec. III-A), kind (`create`, `edit`, `repair`), units (mm), frame convention (Z-up, origin rule), time/edit budget.
- `brief.md`: the request the agent sees, in two styles (as in CADPrompt and Text2CAD-Bench): a functional brief ("bracket to hold a NEMA 17 motor on a 3 mm sheet") and a detailed brief (explicit dimensions). Run each task in both styles; report them separately (CADTests found detailed prompts are harder to fully pass, Sec. 6).
- `seed/` (edit and repair tasks only): a starting feature graph.
- `requirements.yaml`: the machine-checkable requirements (5.2).
- `hidden_checks.py`: extra checks the agent does not see (5.4).
- `reference/`: one valid reference model, used only to validate tests and for secondary similarity metrics. The agent never sees it.

Task kinds and proposed mix for 40 tasks:
- 20 `create` tasks (8 T1-T2, 12 T2-T3).
- 14 `edit` tasks: start from a seed and ask for a change ("change to 4 x M5 clearance holes on an 80 mm bolt circle", "fillet only the outer vertical edges 2 mm", "shell to 2 mm wall, open top"). This tests F6 directly. Requirements include both the change and preservation of everything else.
- 6 `repair` tasks: seed has a known defect (cut that does not cut, hole that breaks an edge, two disjoint bodies); the brief says only "the part fails requirement X".

### 5.2 How requirements are expressed
Each requirement has: `id`, a plain-English `text` (shown to the agent), a typed `check`, a tolerance, and a `group` (so several checks can verify one requirement, as in CADTests' requirement groups, Sec. 5). Proposed check types, chosen to cover the CADTests categories (validity, topology, geometry type, dimension/ratio, volume, spatial relation):
- validity: `single_valid_solid`, `body_count == n`, `watertight`.
- dimension: `bbox.x == 120 ± 0.1`, `distance(sel_a, sel_b) == d ± t`, `diameter(sel) == d ± t`, `min_wall_thickness >= t`.
- topology / feature: `hole_count(on="top face", kind="through", d=6.6±0.05) == 4`, `face_count(type="cylinder") >= n`, `no_open_slot(holes)` (hole fully enclosed by material).
- spatial relation: `concentric(sel_a, sel_b, tol)`, `centered_on(sel, plane)`, `symmetric_about(plane)`, `pattern(bolt_circle, n=4, pcd=80±0.1)`.
- volume / mass: `volume in [a, b]` (useful against hollow/solid confusion, F5).
- contact / gap: `touching(sel_a, sel_b)`, `gap(sel_a, sel_b) >= c` (catches CADSmith's quadcopter-type gap).
- edit preservation: all requirements of the seed that the brief does not change must still pass; faces of untouched features keep the same geometry within tolerance.
- Selectors in checks should use geometric definitions ("the largest planar face with normal +Z") rather than coalesce feature names, so the checks do not depend on how the agent built the part and stay valid if the agent chooses another construction (inference; same reason CADTests is reference-free).

Validate every requirement suite before use, following CADTests (https://arxiv.org/html/2605.07807 Sec. 4-5):
1. All checks pass on the reference model and on transformed copies (translate; rotate 90 degrees when orientation is not specified).
2. Build 5-10 "mutants" per task (change one dimension, delete a feature, flip cut to join, move a hole to break an edge, split into two bodies). Each mutant must fail at least one check. Report the mutation score of the suite; target above 0.9 (CADTests reached 0.91-0.93 after refinement, Table 2).
3. Have a human review the suite against the brief, because LLM-drafted suites alone caught only about 65% of mutants (CADTests Table 1).

### 5.3 Success metrics
Primary:
- **Task pass rate**: a task passes only if all visible and hidden checks pass at the end (same strictness as CADTests Pass-Rate and Query2CAD's exact success). Count crashes, timeouts and invalid output as failures (CADTests Sec. 5 does this; older work excluded them, which inflates numbers).
- **Requirement score**: mean fraction of requirement groups passed (CADTests RS).
- **Reliability**: run each task 3-5 times; report mean pass@1 and pass^k (all k runs pass). Note (my calculation): with 40 tasks and one run, the 95% interval on a 50% pass rate is about ±15 points (1.96 x sqrt(0.25/40)), so single runs cannot separate small design changes.

Efficiency (agent-specific; none of the surveyed papers report these for a tool-based agent, except CADSmith's mean 0.13 refinement iterations):
- edits (feature-graph mutations) to success; total tool calls; failed tool calls; tokens and wall time; number of renders requested.
- recovery rate: fraction of tool errors after which the agent reached success (measures actionable errors).
- regression count on edit tasks: checks that passed on the seed and failed at any later step.

Secondary / diagnostic only:
- Geometric similarity to the reference: IoU after alignment and Chamfer distance in absolute mm (CADSmith uses absolute mm + ICP, Sec. IV; CAD-Coder's IOU_best handles axis ambiguity, https://arxiv.org/html/2505.14646 Sec. 3.3.2). Use it only on fully specified detailed briefs, because a single reference wrongly penalizes valid designs (CADTests Sec. 2).
- Per-category accuracy (validity, topology, dimension, volume, spatial, edit preservation), to see which failure modes F1-F9 remain.
- Human spot check of 10-20% of passing runs for near-misses that all checks missed (the CADSmith gap case). Each confirmed miss becomes a new check.

### 5.4 Visible vs hidden checks
- Coalesce has a "check requirements" tool, so the agent can run the visible checks. That is intended use, but it lets the agent overfit to the checks.
- Keep a hidden set per task: (a) the same requirement checked a second way (e.g. hole count by face count and by ray-cast through the part), (b) default sanity checks (single valid solid, no zero-thickness faces, no open slots), (c) preservation checks on edit tasks.
- Report "visible-pass but hidden-fail" as its own number; it measures check gaming and missing coverage.

### 5.5 Where to get 30-50 parts
1. Private, hand-authored parts (target 20-25): common machine-design families — L-brackets and motor mounts, flanges and bolt circles, shafts with keyways and grooves, plates with counterbored and tapped hole patterns, enclosures with bosses and lids, spacers and bushings, pulleys, hinges, sheet-like parts with slots. Take dimensions from standard tables (clearance holes, counterbores, fastener sizes) and real product needs. This is the best contamination defense because the parts and briefs have never been public.
2. NIST MBE PMI test models (target 4-6): FTC/CTC/STC STEP files with full GD&T; NIST says they "can be used without any restrictions" (https://www.nist.gov/ctl/smart-connected-systems-division/smart-connected-manufacturing-systems-group/mbe-pmi-0). The PMI (dimensions, hole callouts, datums) converts directly into requirements. They are harder than most research benchmarks (inference). The STEP is public, but a construction sequence in coalesce's feature-graph form is not (inference: low memorization risk).
3. Public benchmark parts for comparability (target 6-10, kept as a separate "public" split): a few CADPrompt/CADTestBench items (https://github.com/Kamel773/CAD_Code_Generation, https://arxiv.org/abs/2605.07807) and CADSmith T2/T3 items (https://github.com/jabarkle/CADSmith). Change the dimensions and wording so the agent cannot reuse a memorized script, but keep the original version too, to measure the contamination gap.
4. Edit and repair seeds: build from parts in groups 1-2.
- Do not use DeepCAD / Text2CAD data as the main source: mostly simple sketch-extrude parts biased to boxes and cylinders (Text2CAD Sec. 6), noisy machine prompts (Text2CAD Sec. 15), heavy use in model training data, and a reported (unverified) about 14% train-test geometric duplication. Do not use Fusion 360 Gallery for a product eval: the license is non-commercial research only (https://github.com/AutodeskAILab/Fusion360GalleryDataset/blob/master/LICENSE.md).

### 5.6 Use the eval to test coalesce's design choices
Run the same tasks with tools switched on or off, because the papers disagree on what helps, and their setups differ from coalesce's:
- renders on vs off (CADSmith says on helps on complex parts; CADTests says no gain);
- structured queries rich vs minimal (bbox/volume only, like CADSmith's kernel metrics);
- semantic selectors vs raw face/edge ids;
- actionable errors vs bare error messages;
- requirement-check tool visible vs not (CADTests shows agent-run tests help).
Report pass-rate and edits-to-success differences with paired per-task comparisons.

### 5.7 Contamination controls
- Keep private tasks out of public repos, issues, chat logs and docs; add a canary string to every task file so leaks can be found later (practice from LLM benchmark hygiene; inference, no CAD paper cites it).
- Make each private task a small generator with parameters (dimensions, hole counts, pattern sizes) and draw fresh values at eval time; keep a fixed seed set for regression tracking. Procedural generation is also how CAD-Recode avoided dataset limits (https://arxiv.org/html/2412.14042 Sec. 3.2).
- Record the authoring date of each task; compare models released before and after.
- Report private and public splits separately; a large gap on the public split suggests memorization.
- Run checks outside the agent sandbox; never expose references or hidden checks to the agent.
- Retire and replace about 20% of tasks per quarter, and move retired tasks to the public split.
- Coalesce's feature-graph API is new, so no model has seen solutions written in it (inference). Contamination risk is in the part designs and briefs, not in the API.

## 6. Things I could not verify
- CAD-LLM (Autodesk, 2023): could not open the PDF; metrics named only from a search summary; no results reported here.
- DeepCAD about 14% train-test duplicate figure: from a search summary of an ECCV 2024 supplement PDF I could not open.
- Exact numeric tables not extracted for CADFusion (Table 1), CADReasoner (Tables 1-2), Seek-CAD, EvoCAD, Text2CAD-Bench, MUSE, cadrille, CAD-Editor; only text claims are cited for them.
- CADTests author list and venue: not extracted.
- Zoo's "50% to 16%" KCL error-rate figure is company-reported, not a benchmark.
