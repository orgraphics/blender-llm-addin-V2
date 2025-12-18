import sys
import os
import re
import ast
import textwrap
import bpy
import threading
import queue
import time
from ollama import chat, ChatResponse
import openai
from openai import OpenAI

# --- CONFIGURATION & PATHS ---
MODULES_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "modules")
if MODULES_PATH not in sys.path:
    sys.path.append(MODULES_PATH)

# Initialize OpenAI Client
client = OpenAI(api_key='<input your OpenAI API key>') 

# Known Ollama cloud models (these require special handling)
CLOUD_MODELS = [
    "qwen3-coder:480b-cloud",
    "gpt-oss:120b-cloud",
    "gpt-oss:20b-cloud",
    "deepseek-v3.1:671b-cloud"
]

def is_cloud_model(model_name):
    """Check if model is a cloud model."""
    return model_name in CLOUD_MODELS or model_name.endswith('-cloud')

# --- GLOBAL STATE ---
execution_queue = queue.Queue()
conversation_history = []

# --- DEFENSIVE CODING RULES ---
DEFENSIVE_RULES = """
CRITICAL RULES - BLENDER PYTHON CODE GENERATION:

1. EXISTENCE CHECKS - MANDATORY:
   - NEVER assume properties, nodes, sockets, modifiers exist
   - ALWAYS use .get() or 'in' checks before accessing
   - Example: node.inputs.get("Roughness") or "Roughness" in node.inputs

2. OBJECT & CONTEXT VALIDATION:
   - Check bpy.context.active_object is not None
   - Check object has .data attribute when needed
   - Verify material exists before accessing

3. MATERIAL & NODE SAFETY:
   - Set material.use_nodes = True before node operations
   - Create Principled BSDF if missing: nodes.get("Principled BSDF") or nodes.new("ShaderNodeBsdfPrincipled")
   - Check socket exists before setting: if "Base Color" in bsdf.inputs: bsdf.inputs["Base Color"].default_value = ...

4. RENDER ENGINE COMPATIBILITY:
   - Set bpy.context.scene.render.engine = 'CYCLES' or 'BLENDER_EEVEE' before material operations
   - Some features need specific engines

5. MODIFIER SAFETY:
   - Check if modifier exists: obj.modifiers.get("ModifierName")
   - Create if missing: mod = obj.modifiers.new("Name", 'TYPE')

6. NO CRASHES ALLOWED:
   - Wrap risky operations in try/except if necessary
   - Use defensive checks to prevent KeyError, AttributeError, TypeError
   - Fail gracefully - skip unsupported features silently

7. CODE STRUCTURE:
   - Start with validation checks
   - Then create missing components
   - Finally apply modifications
   - Use clear variable names

8. EXAMPLE PATTERN:
```python
obj = bpy.context.active_object
if obj and obj.type == 'MESH':
    if not obj.data.materials:
        mat = bpy.data.materials.new("Material")
        obj.data.materials.append(mat)
    else:
        mat = obj.data.materials[0]
    
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    
    bsdf = nodes.get("Principled BSDF")
    if not bsdf:
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    
    if "Base Color" in bsdf.inputs:
        bsdf.inputs["Base Color"].default_value = (1, 0, 0, 1)
```

OUTPUT REQUIREMENTS:
- Return ONLY executable Python code
- No markdown formatting, no explanations
- Wrap in ```python if requested, otherwise raw code
- Every line must be crash-proof
"""

# --- HELPER FUNCTIONS ---

def get_ollama_models():
    """Return a list of downloaded local Ollama models."""
    try:
        import json
        result = os.popen('ollama list --json').read()
        data = json.loads(result)
        return [m['name'] for m in data]
    except Exception as e:
        print(f"Error getting Ollama models: {e}")
        return []

def describe_scene():
    """Return a textual summary of all objects in the current Blender scene."""
    desc = []
    objects = list(bpy.context.scene.objects)[:50]
    
    for obj in objects:
        try:
            obj_type = obj.type
            loc = tuple(round(c, 2) for c in obj.location)
            
            if obj.rotation_mode == 'QUATERNION':
                rot = "Quaternion" 
            else:
                rot = tuple(round(c, 2) for c in obj.rotation_euler)
            
            scale = tuple(round(c, 2) for c in obj.scale)
            mat = obj.active_material.name if obj.active_material else "None"
            
            line = f"- Name: {obj.name} | Type: {obj_type} | Loc: {loc} | Rot: {rot} | Scale: {scale} | Mat: {mat}"
            desc.append(line)
        except Exception:
            continue
            
    if not desc:
        return "Scene is empty."
    return "\n".join(desc)

def get_scene_statistics():
    """Get detailed scene statistics."""
    stats = {
        'total_objects': len(bpy.context.scene.objects),
        'meshes': len([o for o in bpy.context.scene.objects if o.type == 'MESH']),
        'lights': len([o for o in bpy.context.scene.objects if o.type == 'LIGHT']),
        'cameras': len([o for o in bpy.context.scene.objects if o.type == 'CAMERA']),
        'empties': len([o for o in bpy.context.scene.objects if o.type == 'EMPTY']),
    }
    return stats

def preprocess_code(text: str) -> str:
    """Extracts Python code from Markdown and cleans it."""
    try:
        # Try multiple patterns for code extraction
        patterns = [
            r'```python\n(.*?)```',
            r'```py\n(.*?)```',
            r'```\n(.*?)```',
        ]
        
        code = None
        for pattern in patterns:
            match = re.search(pattern, text, re.DOTALL)
            if match:
                code = match.group(1).strip()
                break
        
        # If no code block found, check if entire response is code
        if not code:
            # Check if it looks like Python code
            if 'import bpy' in text or 'bpy.' in text:
                code = text.strip()
            else:
                print(f"[DEBUG] No code found in response. Full text:\n{text}")
                return ''
            
        code = code.replace('\t', '    ')
        code = textwrap.dedent(code)
        
        # Check for dangerous libraries
        unsafe_libs = ["shutil", "subprocess", "ctypes", "socket"]
        for lib in unsafe_libs:
            if f"import {lib}" in code or f"from {lib}" in code:
                raise Exception(f"Unsafe library detected: {lib}")
        
        # Syntax check
        ast.parse(code)
        print(f"[DEBUG] Code validated successfully. Length: {len(code)} chars")
        return code
    except SyntaxError as e:
        print(f"[DEBUG] Syntax error in generated code: {e}")
        return ''
    except Exception as e:
        print(f"[DEBUG] Code processing error: {e}")
        return ''

# --- AI AGENTS ---

def openai_agent(prompt, system_prompt, model="gpt-4o", mode="code"):
    """Unified OpenAI agent for both code generation and Q&A."""
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1 if mode == "code" else 0.7,
            max_tokens=2048
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Error: {str(e)}"

def llm_agent(option, prompt, system_prompt=""):
    """Unified Ollama agent for both code generation and Q&A."""
    try:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        
        # Cloud models require different parameters
        if is_cloud_model(option):
            response = chat(
                model=option,
                messages=messages,
                options={
                    'temperature': 0.1,
                    'num_predict': 2048,
                }
            )
        else:
            response = chat(model=option, messages=messages)
        
        if response and isinstance(response, ChatResponse):
            content = response.message.content
            print(f"[DEBUG] Model response length: {len(content)} chars")
            print(f"[DEBUG] First 200 chars: {content[:200]}")
            return content
        else:
            return "Error: Failed to get valid response"
    except Exception as e:
        print(f"[DEBUG] Ollama error: {str(e)}")
        return f"Error: {str(e)}"

# --- BACKGROUND WORKERS ---

def ai_code_worker_thread(model_option, user_prompt, scene_context_str):
    """Background worker for code generation."""
    update_log("Analyzing scene and generating code...")
    
    # Enhanced system prompt with defensive rules
    system_prompt = f"""You are an expert Blender Python (bpy) code generator.

{DEFENSIVE_RULES}

Current Scene State:
{scene_context_str}

RESPONSE FORMAT:
- Output ONLY valid, executable Python code
- Wrap code in ```python blocks
- No explanations outside code blocks
- Every operation must be crash-proof
"""
    
    user_message = f"""Task: {user_prompt}

Requirements:
1. Validate all objects, materials, nodes before use
2. Create missing components safely
3. Use defensive checks (.get(), 'in' operator)
4. Never crash - handle all edge cases
5. Set render engine if modifying materials

Generate crash-proof Blender Python code:"""
    
    output_text = ""
    try:
        if model_option == 'chatgpt':
            output_text = openai_agent(user_message, system_prompt, mode="code")
        else:
            output_text = llm_agent(model_option, user_message, system_prompt)
        
        print(f"[DEBUG] Raw AI output:\n{output_text[:500]}...")
            
        update_log("Processing code...")
        clean_code = preprocess_code(output_text)
        
        if not clean_code:
            update_log("Error: No valid code generated. Check console for details.")
            return

        execution_queue.put(clean_code)
        bpy.app.timers.register(process_queue_timer)
        
    except Exception as e:
        update_log(f"Error during generation: {str(e)}")
        print(f"[DEBUG] Full error: {e}")

def ai_qa_worker_thread(model_option, user_question, scene_context_str):
    """Background worker for Q&A about the scene."""
    update_log("Analyzing scene and preparing answer...")
    
    stats = get_scene_statistics()
    stats_str = f"Scene Statistics: {stats['total_objects']} total objects ({stats['meshes']} meshes, {stats['lights']} lights, {stats['cameras']} cameras)"
    
    system_prompt = (
        'You are a helpful Blender scene analysis assistant.\n'
        'Provide clear, concise answers about the Blender scene.\n'
        'Use the scene context to give accurate information.\n'
        'If asked about specific objects, reference them by name.\n'
        'Be conversational and helpful.'
    )
    
    full_prompt = (
        f'SCENE CONTEXT:\n{scene_context_str}\n\n'
        f'{stats_str}\n\n'
        f'USER QUESTION: {user_question}'
    )
    
    output_text = ""
    if model_option == 'chatgpt':
        output_text = openai_agent(full_prompt, system_prompt, mode="qa")
    else:
        output_text = llm_agent(model_option, full_prompt, system_prompt)
    
    # Store in conversation history
    conversation_history.append({
        'question': user_question,
        'answer': output_text
    })
    
    update_response(output_text)
    update_log("Answer ready!")

def update_log(message):
    """Update status log."""
    print(f"[AI LOG]: {message}")
    def update_ui():
        bpy.context.scene.ai_status_log = message
    bpy.app.timers.register(update_ui)

def update_response(message):
    """Update Q&A response field."""
    def update_ui():
        bpy.context.scene.ai_response = message
    bpy.app.timers.register(update_ui)

# --- MAIN THREAD TIMER ---

def process_queue_timer():
    """Checks queue for code to execute."""
    while not execution_queue.empty():
        code = execution_queue.get()
        try:
            update_log("Executing Code...")
            exec(code)
            update_log("Done! Scene updated.")
        except Exception as e:
            update_log(f"Execution Error: {e}")
            print(f"Failed Code:\n{code}")
            
    return None

# --- BLENDER UI CLASSES ---

class OBJECT_PT_CustomPanel(bpy.types.Panel):
    bl_label = "AI Scene Assistant"
    bl_idname = "OBJECT_PT_custom_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "AI Assistant"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # Model Section
        box_model = layout.box()
        box_model.label(text="AI Model", icon='PREFERENCES')
        box_model.prop(scene, "ai_model", text="")
        
        layout.separator()

        # Mode Selection
        box_mode = layout.box()
        box_mode.label(text="Operation Mode", icon='MODIFIER')
        row = box_mode.row(align=True)
        row.prop(scene, "ai_mode", expand=True)
        
        layout.separator()

        # CODE GENERATION MODE
        if scene.ai_mode == 'CODE':
            box_code = layout.box()
            box_code.label(text="Code Generation", icon='SCRIPTPLUGINS')
            
            row = box_code.row()
            row.scale_y = 1.5 
            row.prop(scene, "user_prompt", text="", icon='NONE')
            
            row = box_code.row()
            row.scale_y = 1.3
            row.operator("object.submit_prompt", text="Generate Code", icon='PLAY')
        
        # Q&A MODE
        else:
            box_qa = layout.box()
            box_qa.label(text="Ask About Scene", icon='QUESTION')
            
            row = box_qa.row()
            row.scale_y = 1.5 
            row.prop(scene, "user_question", text="", icon='NONE')
            
            row = box_qa.row()
            row.scale_y = 1.3
            row.operator("object.ask_question", text="Ask Question", icon='VIEWZOOM')
            
            layout.separator()
            
            # Response Display
            if scene.ai_response:
                box_response = layout.box()
                box_response.label(text="AI Response:", icon='TEXT')
                
                response_text = scene.ai_response
                lines = textwrap.wrap(response_text, width=40)
                for line in lines[:20]:  # Limit to 20 lines
                    box_response.label(text=line)
                
                if len(lines) > 20:
                    box_response.label(text="... (see console for full response)")

        layout.separator()

        # Status Log
        box_log = layout.box()
        box_log.label(text="Status:", icon='CONSOLE')
        
        log_text = scene.ai_status_log
        lines = textwrap.wrap(log_text, width=40)
        for line in lines:
            box_log.label(text=line)
        
        layout.separator()
        
        # Utility Buttons
        row = layout.row(align=True)
        row.operator("object.clear_history", text="Clear History", icon='X')
        row.operator("object.show_scene_info", text="Scene Info", icon='INFO')

class OBJECT_OT_SubmitPrompt(bpy.types.Operator):
    """Generate Blender Python code from natural language prompt"""
    bl_label = "Submit Prompt"
    bl_idname = "object.submit_prompt"

    def execute(self, context):
        option = context.scene.ai_model
        user_prompt = context.scene.user_prompt
        
        if not user_prompt:
            self.report({'WARNING'}, "Please enter a prompt")
            return {'CANCELLED'}

        scene_desc = describe_scene()
        context.scene.ai_status_log = "Reading scene..."

        t = threading.Thread(target=ai_code_worker_thread, args=(option, user_prompt, scene_desc))
        t.daemon = True
        t.start()

        return {'FINISHED'}

class OBJECT_OT_AskQuestion(bpy.types.Operator):
    """Ask questions about the current Blender scene"""
    bl_label = "Ask Question"
    bl_idname = "object.ask_question"

    def execute(self, context):
        option = context.scene.ai_model
        user_question = context.scene.user_question
        
        if not user_question:
            self.report({'WARNING'}, "Please enter a question")
            return {'CANCELLED'}

        scene_desc = describe_scene()
        context.scene.ai_status_log = "Analyzing scene..."
        context.scene.ai_response = ""

        t = threading.Thread(target=ai_qa_worker_thread, args=(option, user_question, scene_desc))
        t.daemon = True
        t.start()

        return {'FINISHED'}

class OBJECT_OT_ClearHistory(bpy.types.Operator):
    """Clear conversation history and response"""
    bl_label = "Clear History"
    bl_idname = "object.clear_history"

    def execute(self, context):
        global conversation_history
        conversation_history = []
        context.scene.ai_response = ""
        context.scene.ai_status_log = "History cleared. Ready."
        self.report({'INFO'}, "Conversation history cleared")
        return {'FINISHED'}

class OBJECT_OT_ShowSceneInfo(bpy.types.Operator):
    """Display detailed scene information"""
    bl_label = "Show Scene Info"
    bl_idname = "object.show_scene_info"

    def execute(self, context):
        stats = get_scene_statistics()
        info = (
            f"Scene Statistics:\n"
            f"Total Objects: {stats['total_objects']}\n"
            f"Meshes: {stats['meshes']}\n"
            f"Lights: {stats['lights']}\n"
            f"Cameras: {stats['cameras']}\n"
            f"Empties: {stats['empties']}"
        )
        self.report({'INFO'}, info)
        print(f"\n{info}\n")
        print(f"Full Scene Context:\n{describe_scene()}")
        return {'FINISHED'}

# --- REGISTRATION ---

def register():
    bpy.utils.register_class(OBJECT_PT_CustomPanel)
    bpy.utils.register_class(OBJECT_OT_SubmitPrompt)
    bpy.utils.register_class(OBJECT_OT_AskQuestion)
    bpy.utils.register_class(OBJECT_OT_ClearHistory)
    bpy.utils.register_class(OBJECT_OT_ShowSceneInfo)

    local_models = get_ollama_models()
    model_items = [('chatgpt', "ChatGPT (OpenAI)", "")]
    model_items += [(m, m, "") for m in local_models]
    model_items += [(c, c, "") for c in CLOUD_MODELS if c not in local_models]

    bpy.types.Scene.ai_model = bpy.props.EnumProperty(
        name="AI Model", 
        items=model_items,
        description="Select AI model for code generation and Q&A"
    )
    
    bpy.types.Scene.ai_mode = bpy.props.EnumProperty(
        name="Mode",
        items=[
            ('CODE', "Code Generation", "Generate Python code to modify the scene"),
            ('QA', "Q&A", "Ask questions about the scene")
        ],
        default='CODE',
        description="Choose between code generation or scene analysis"
    )
    
    bpy.types.Scene.user_prompt = bpy.props.StringProperty(
        name="Code Prompt", 
        maxlen=1024,
        description="Describe what you want to create or modify"
    )
    
    bpy.types.Scene.user_question = bpy.props.StringProperty(
        name="Question", 
        maxlen=1024,
        description="Ask about objects, materials, or scene properties"
    )
    
    bpy.types.Scene.ai_response = bpy.props.StringProperty(
        name="AI Response",
        default="",
        description="AI's answer to your question"
    )
    
    bpy.types.Scene.ai_status_log = bpy.props.StringProperty(
        name="Log", 
        default="Ready. Select a mode to begin."
    )

def unregister():
    bpy.utils.unregister_class(OBJECT_PT_CustomPanel)
    bpy.utils.unregister_class(OBJECT_OT_SubmitPrompt)
    bpy.utils.unregister_class(OBJECT_OT_AskQuestion)
    bpy.utils.unregister_class(OBJECT_OT_ClearHistory)
    bpy.utils.unregister_class(OBJECT_OT_ShowSceneInfo)
    
    del bpy.types.Scene.ai_model
    del bpy.types.Scene.ai_mode
    del bpy.types.Scene.user_prompt
    del bpy.types.Scene.user_question
    del bpy.types.Scene.ai_response
    del bpy.types.Scene.ai_status_log

if __name__ == "__main__":
    register()
