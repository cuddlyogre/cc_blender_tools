import bpy, bpy_extras
import bpy_extras.view3d_utils as v3d
import atexit
from enum import IntEnum
import os, socket, time, select, struct, json
import subprocess
from mathutils import Vector, Quaternion, Matrix
from . import importer, rigging, bones, geom, colorspace, utils, vars


BLENDER_PORT = 9334
UNITY_PORT = 9335
RL_PORT = 9333
HANDSHAKE_TIMEOUT_S = 60
KEEPALIVE_TIMEOUT_S = 300
PING_INTERVAL_S = 120
TIMER_INTERVAL = 1/30
MAX_CHUNK_SIZE = 32768
SERVER_ONLY = False
CLIENT_ONLY = True
CHARACTER_TEMPLATE: list = None
MAX_RECEIVE = 24
USE_PING = False
USE_KEEPALIVE = False
SOCKET_TIMEOUT = 5.0

class OpCodes(IntEnum):
    NONE = 0
    HELLO = 1
    PING = 2
    STOP = 10
    DISCONNECT = 11
    NOTIFY = 50
    MORPH = 90
    MORPH_UPDATE = 91
    CHARACTER = 100
    CHARACTER_UPDATE = 101
    PROP = 102
    PROP_UPDATE = 103
    RIGIFY = 110
    TEMPLATE = 200
    POSE = 210
    POSE_FRAME = 211
    SEQUENCE = 220
    SEQUENCE_FRAME = 221
    SEQUENCE_END = 222
    LIGHTS = 230
    CAMERA_SYNC = 231


class LinkActor():
    name: str = "Name"
    chr_cache = None
    link_id: str = "1234567890"
    bones: list = []
    expressions: list = []
    visemes: list = []
    morphs: list = []
    cache: dict = None

    def __init__(self, chr_cache):
        self.name = chr_cache.character_name
        self.link_id = chr_cache.link_id
        self.chr_cache = chr_cache
        return

    def get_chr_cache(self):
        props = bpy.context.scene.CC3ImportProps
        if not self.chr_cache:
            self.chr_cache = props.get_link_character_cache(self.link_id)
        return self.chr_cache

    def get_armature(self):
        if self.chr_cache:
            return self.chr_cache.get_armature()
        return None

    def get_mesh_objects(self):
        objects = None
        if self.chr_cache:
            objects = self.chr_cache.get_all_objects(include_armature=False,
                                                     include_children=True,
                                                     of_type="MESH")
        return objects

    def object_has_sequence_shape_keys(self, obj):
        if obj.data.shape_keys and obj.data.shape_keys.key_blocks:
            for expression_name in self.expressions:
                if expression_name in obj.data.shape_keys.key_blocks:
                    return True
            for viseme_name in self.visemes:
                if viseme_name in obj.data.shape_keys.key_blocks:
                    return True
        return False

    def get_sequence_objects(self):
        objects = []
        if self.chr_cache:
            all_objects = self.chr_cache.get_all_objects(include_armature=False,
                                                     include_children=True,
                                                     of_type="MESH")
            for obj in all_objects:
                if self.object_has_sequence_shape_keys(obj):
                    objects.append(obj)
        return objects

    def set_template(self, bones, expressions, visemes, morphs):
        self.bones = bones
        self.expressions = expressions
        self.visemes = visemes
        self.morphs = morphs

    def clear_template(self):
        self.bones = None

    def set_cache(self, cache):
        self.cache = cache

    def clear_cache(self):
        self.cache = None

    def update(self, new_name, new_link_id):
        self.name = new_name
        self.link_id = new_link_id
        chr_cache = self.get_chr_cache()
        if chr_cache:
            chr_cache.character_name = new_name
            chr_cache.link_id = new_link_id


class LinkData():
    link_host: str = "localhost"
    link_host_ip: str = "127.0.0.1"
    link_target: str = "BLENDER"
    link_port: int = 9333
    actors: list = []
    # Pose Props
    pose_frame: int = 0
    pose_actors: list = None
    # Sequence Props
    sequence_current_frame: int = 0
    sequence_start_frame: int = 0
    sequence_end_frame: int = 0
    sequence_actors: list = None

    def __init__(self):
        return

    def reset(self):
        self.actors = []
        self.sequence_actors = None
        self.pose_actors = None

    def get_actor(self, link_id) -> LinkActor:
        props = bpy.context.scene.CC3ImportProps
        for actor in self.actors:
            if actor.link_id == link_id:
                return actor
        chr_cache = props.get_link_character_cache(link_id)
        if chr_cache:
            return self.add_actor(chr_cache)
        return None

    def add_actor(self, chr_cache) -> LinkActor:
        for actor in self.actors:
            if actor.chr_cache == chr_cache:
                return actor
        actor = LinkActor(chr_cache)
        self.actors.append(actor)
        return actor


LINK_DATA = LinkData()


def encode_from_json(json_data) -> bytearray:
    json_string = json.dumps(json_data)
    json_bytes = bytearray(json_string, "utf-8")
    return json_bytes


def decode_to_json(data) -> dict:
    text = data.decode("utf-8")
    json_data = json.loads(text)
    return json_data


def pack_string(s) -> bytearray:
    buffer = bytearray()
    buffer += struct.pack("!I", len(s))
    buffer += bytes(s, encoding="utf-8")
    return buffer


def unpack_string(buffer, offset=0):
    length = struct.unpack_from("!I", buffer, offset)[0]
    offset += 4
    string: bytearray = buffer[offset:offset+length]
    offset += length
    return offset, string.decode(encoding="utf-8")


def get_link_data_path():
    local_path = utils.local_path()
    blend_file_name = utils.blend_file_name()
    data_path = ""
    if local_path and blend_file_name:
        data_path = os.path.join(local_path, "Data Link", blend_file_name)
    return data_path


def make_datalink_import_rig(chr_cache, character_template):
    """Creates or re-uses and existing datalink pose rig for the character.
       This uses a pre-generated character template (list of bones in the character)
       sent from CC/iC to avoid encoding the bone names into the pose data stream."""

    # get character armature
    chr_rig: bpy.types.Object = chr_cache.get_armature()
    chr_rig.hide_set(False)

    if utils.object_exists_is_armature(chr_cache.rig_datalink_rig):
        chr_cache.rig_datalink_rig.hide_set(False)
        return chr_cache.rig_datalink_rig

    no_constraints = True if chr_cache.rigified else False

    rig_name = f"{chr_cache.character_name}_Link_Rig"

    # create pose armature
    datalink_rig = utils.get_armature(rig_name)
    if not datalink_rig:
        datalink_rig = utils.create_reuse_armature(rig_name)
        edit_bone: bpy.types.EditBone
        arm: bpy.types.Armature = datalink_rig.data
        if utils.edit_mode_to(datalink_rig):
            for sk_bone_name in character_template:
                edit_bone = arm.edit_bones.new(sk_bone_name)
                edit_bone.head = Vector((0,0,0))
                edit_bone.tail = Vector((0,1,0))
                edit_bone.align_roll(Vector((0,0,1)))
                edit_bone.length = 0.1

        utils.object_mode_to(datalink_rig)

        # constraint character armature
        print("CONSTRAIN")
        if not no_constraints:
            for sk_bone_name in character_template:
                chr_bone_name = bones.find_target_bone_name(chr_rig, sk_bone_name)
                print(f"BONE: {chr_bone_name}")
                if chr_bone_name:
                    pivot_bone = bones.find_pivot_bone(chr_rig, chr_bone_name)
                    bones.add_copy_location_constraint(datalink_rig, chr_rig, sk_bone_name, chr_bone_name)
                    bones.add_copy_rotation_constraint(datalink_rig, chr_rig, sk_bone_name, chr_bone_name)
                    if pivot_bone:
                        bones.add_copy_location_constraint(datalink_rig, chr_rig, sk_bone_name, pivot_bone.name)
                        bones.add_copy_rotation_constraint(datalink_rig, chr_rig, sk_bone_name, pivot_bone.name)
                else:
                    utils.log_warn(f"Could not find bone: {sk_bone_name} in character rig!")
        utils.safe_set_action(datalink_rig, None)

    utils.object_mode_to(datalink_rig)
    datalink_rig.hide_set(True)

    chr_cache.rig_datalink_rig = datalink_rig

    if chr_cache.rigified:
        # a rigified character must retarget the link rig, but...
        # the link rig doesn't have a valid bind pose, so the retargeting rig
        # can't use it as a source rig for the roll axes on the ORG bones,
        # so we use the original ones for the character type (option to_original_rig)
        # (data on the original bones is added the ORG bones during rigify process)
        rigging.adv_retarget_remove_pair(None, chr_cache)
        if not chr_cache.rig_retarget_rig:
            rigging.adv_retarget_pair_rigs(None, chr_cache,
                                        source_rig_override=datalink_rig,
                                        to_original_rig=True)

    return datalink_rig


def remove_datalink_import_rig(actor: LinkActor):
    if actor:
        chr_cache = actor.get_chr_cache()
        chr_rig = actor.get_armature()

        if utils.object_exists_is_armature(chr_cache.rig_datalink_rig):

            if chr_cache.rigified:
                rigging.adv_retarget_remove_pair(None, chr_cache)

            else:
                # remove all contraints on the character rig
                if utils.object_exists(chr_rig):
                    chr_rig.hide_set(False)
                    if utils.object_mode_to(chr_rig):
                        for pose_bone in chr_rig.pose.bones:
                            bones.clear_constraints(chr_rig, pose_bone.name)

            utils.delete_armature_object(chr_cache.rig_datalink_rig)
            chr_cache.rig_datalink_rig = None

        #rigging.reset_shape_keys(chr_cache)
        utils.object_mode_to(chr_rig)


def set_actor_expression_weight(objects, expression_name, weight):
    if objects:
        obj: bpy.types.Object
        for obj in objects:
            if expression_name in obj.data.shape_keys.key_blocks:
                obj.data.shape_keys.key_blocks[expression_name].value = weight


VISEME_NAME_MAP = {
    "Open": "V_Open",
    "Explosive": "V_Explosive",
    "Upper Dental": "V_Dental_Lip",
    "Tight O": "V_Tight_O",
    "Pucker": "V_Tight",
    "Wide": "V_Wide",
    "Affricate": "V_Affricate",
    "Lips Parted": "V_Lip_Open",
    "Tongue Up": "V_Tongue_up",
    "Tongue Raised": "V_Tongue_Raise",
    "Tongue Out": "V_Tongue_Out",
    "Tongue Narrow": "V_Tongue_Narrow",
    "Tongue Lower": "V_Tongue_Lower",
    "Tongue Curl-U": "V_Tongue_Curl_U",
    "Tongue Curl-D": "V_Tongue_Curl_D",
}

def set_actor_viseme_weight(objects, viseme_name, weight):
    if objects:
        for obj in objects:
            if obj.data.shape_keys and obj.data.shape_keys.key_blocks:
                if viseme_name in VISEME_NAME_MAP:
                    blender_viseme_name = VISEME_NAME_MAP[viseme_name]
                    if blender_viseme_name in obj.data.shape_keys.key_blocks:
                        obj.data.shape_keys.key_blocks[blender_viseme_name].value = weight


def ensure_current_frame(current_frame):
    if bpy.context.scene.frame_current != current_frame:
        bpy.context.scene.frame_current = current_frame
    return current_frame


def next_frame(current_frame=None):
    if current_frame is None:
        current_frame = bpy.context.scene.frame_current
    fps = bpy.context.scene.render.fps
    end_frame = bpy.context.scene.frame_end
    current_frame = min(end_frame, current_frame + 1)
    bpy.context.scene.frame_current = current_frame
    return current_frame


#fcurve.keyframe_points.insert

def reset_action(chr_cache):
    if chr_cache:
        rig = chr_cache.get_armature()
        if rig:
            action = utils.safe_get_action(rig)
            action.fcurves.clear()


def create_fcurves_cache(count, indices, defaults):
    curves = []
    cache = {
        "count": count,
        "indices": indices,
        "curves": curves,
    }
    for i in range(0, indices):
        d = defaults[i]
        cache_data = [d]*(count*2)
        curves.append(cache_data)
    return cache

def prep_rig(actor: LinkActor, start_frame, end_frame):
    """Prepares the character rig for keyframing poses from the pose data stream."""

    if actor and actor.chr_cache:
        chr_cache = actor.get_chr_cache()
        rig = actor.get_armature()
        objects = actor.get_sequence_objects()
        if rig:

            # rig action
            action_name = f"{chr_cache.character_name}_data_link_action"
            if action_name in bpy.data.actions:
                action = bpy.data.actions[action_name]
            else:
                action = bpy.data.actions.new(action_name)
            action.fcurves.clear()
            utils.safe_set_action(rig, action)

            # shape key actions
            if objects:
                for obj in objects:
                    action_name = f"{chr_cache.character_name}_{obj.name}_data_link_action"
                    if action_name in bpy.data.actions:
                        action = bpy.data.actions[action_name]
                    else:
                        action = bpy.data.actions.new(action_name)
                    action.fcurves.clear()
                    utils.safe_set_action(obj.data.shape_keys, action)

            if chr_cache.rigified:
                BAKE_BONE_GROUPS = ["FK", "IK", "Special", "Root"] #not Tweak and Extra
                BAKE_BONE_COLLECTIONS = ["Face", "Torso", "Fingers (Detail)",
                                        "Arm.L (IK)", "Arm.L (FK)", "Leg.L (IK)", "Leg.L (FK)",
                                        "Arm.R (IK)", "Arm.R (FK)", "Leg.R (IK)", "Leg.R (FK)",
                                        "Root"]
                BAKE_BONE_LAYERS = [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,28]
                if utils.object_mode_to(rig):
                    bone : bpy.types.Bone
                    bones.make_bones_visible(rig, collections=BAKE_BONE_COLLECTIONS, layers=BAKE_BONE_LAYERS)
                    for bone in rig.data.bones:
                        bone.select = False
                        if bones.is_bone_in_collections(rig, bone, BAKE_BONE_COLLECTIONS,
                                                                BAKE_BONE_GROUPS):
                            bone.select = True
            else:
                if utils.object_mode_to(rig):
                    bone : bpy.types.Bone
                    for bone in rig.data.bones:
                        bone.hide = False
                        bone.hide_select = False
                        bone.select = True

            # create keyframe cache for animation sequence
            count = end_frame - start_frame + 1
            bone_cache = {}
            expression_cache = {}
            viseme_cache = {}
            morph_cache = {}
            bones = []
            actor_cache = {
                "rig": rig,
                "bones": bone_cache,
                "expressions": expression_cache,
                "visemes": viseme_cache,
                "morphs": morph_cache,
                "start": start_frame,
                "end": end_frame,
            }
            for pose_bone in rig.pose.bones:
                bone_name = pose_bone.name
                bone = rig.data.bones[bone_name]
                if bone.select:
                    print(f" - Bone Cache: {bone_name}")
                    loc_cache = create_fcurves_cache(count, 3, [0,0,0])
                    sca_cache = create_fcurves_cache(count, 3, [1,1,1])
                    rot_cache = create_fcurves_cache(count, 4, [1,0,0,0])
                    bone_cache[bone_name] = {
                        "loc": loc_cache,
                        "sca": sca_cache,
                        "rot": rot_cache,
                    }

            for expression_name in actor.expressions:
                expression_cache[expression_name] = create_fcurves_cache(count, 1, [0])

            for viseme_name in actor.visemes:
                viseme_cache[viseme_name] = create_fcurves_cache(count, 1, [0])

            for morph_name in actor.morphs:
                pass

            actor.set_cache(actor_cache)


def set_frame_range(start, end):
    bpy.data.scenes["Scene"].frame_start = start
    bpy.data.scenes["Scene"].frame_end = end


def set_frame(frame):
    bpy.data.scenes["Scene"].frame_current = frame
    bpy.context.view_layer.update()


def key_frame_pose_visual():
    area = [a for a in bpy.context.screen.areas if a.type=="VIEW_3D"][0]
    with bpy.context.temp_override(area=area):
        bpy.ops.anim.keyframe_insert_menu(type='BUILTIN_KSI_VisualLocRot')


def store_bone_cache_keyframes(actor: LinkActor, frame):
    """Needs to be called after all constraints have been set and all bones in the pose positioned"""

    rig = actor.get_armature()
    start_frame = actor.cache["start"]
    cache_index = (frame - start_frame) * 2
    bone_cache = actor.cache["bones"]
    for bone_name in bone_cache:
        loc_cache = bone_cache[bone_name]["loc"]
        sca_cache = bone_cache[bone_name]["sca"]
        rot_cache = bone_cache[bone_name]["rot"]
        pose_bone: bpy.types.PoseBone = rig.pose.bones[bone_name]
        M: Matrix
        O: Matrix = pose_bone.matrix
        RI: Matrix = pose_bone.bone.matrix_local.inverted()
        if pose_bone.parent:
            PI: Matrix = pose_bone.parent.matrix.inverted()
            PR: Matrix = pose_bone.parent.bone.matrix_local
            M: Matrix = RI @ (PR @ (PI @ O))
        else:
            M: Matrix = RI @ O
        loc = M.to_translation()
        sca = M.to_scale()
        rot = M.to_quaternion()
        for i in range(0, 3):
            curve = loc_cache["curves"][i]
            curve[cache_index] = frame
            curve[cache_index + 1] = loc[i]
        for i in range(0, 3):
            curve = sca_cache["curves"][i]
            curve[cache_index] = frame
            curve[cache_index + 1] = sca[i]
        for i in range(0, 4):
            curve = rot_cache["curves"][i]
            curve[cache_index] = frame
            curve[cache_index + 1] = rot[i]


def store_shape_key_cache_keyframes(actor: LinkActor, frame, expression_weights, viseme_weights, morph_weights):
    start_frame = actor.cache["start"]
    cache_index = (frame - start_frame) * 2

    expression_cache = actor.cache["expressions"]
    for i, expression_name in enumerate(expression_cache):
        curve = expression_cache[expression_name]["curves"][0]
        curve[cache_index] = frame
        curve[cache_index + 1] = expression_weights[i]

    viseme_cache = actor.cache["visemes"]
    for i, viseme_name in enumerate(viseme_cache):
        curve = viseme_cache[viseme_name]["curves"][0]
        curve[cache_index] = frame
        curve[cache_index + 1] = viseme_weights[i]


def write_sequence_actions(actor: LinkActor):
    if actor.cache:
        rig = actor.cache["rig"]
        rig_action = utils.safe_get_action(rig)
        objects = actor.get_sequence_objects()

        if rig_action:
            rig_action.fcurves.clear()
            bone_cache = actor.cache["bones"]
            for bone_name in bone_cache:
                pose_bone: bpy.types.PoseBone = rig.pose.bones[bone_name]
                loc_cache = bone_cache[bone_name]["loc"]
                sca_cache = bone_cache[bone_name]["sca"]
                rot_cache = bone_cache[bone_name]["rot"]
                fcurve: bpy.types.FCurve
                for i in range(0, 3):
                    data_path = pose_bone.path_from_id("location")
                    fcurve = rig_action.fcurves.new(data_path, index=i, action_group="Location")
                    fcurve.keyframe_points.add(loc_cache["count"])
                    fcurve.keyframe_points.foreach_set('co', loc_cache["curves"][i])
                for i in range(0, 3):
                    data_path = pose_bone.path_from_id("scale")
                    fcurve = rig_action.fcurves.new(data_path, index=i, action_group="Scale")
                    fcurve.keyframe_points.add(sca_cache["count"])
                    fcurve.keyframe_points.foreach_set('co', sca_cache["curves"][i])
                for i in range(0, 4):
                    data_path = pose_bone.path_from_id("rotation_quaternion")
                    fcurve = rig_action.fcurves.new(data_path, index=i, action_group="Rotation Quaternion")
                    fcurve.keyframe_points.add(rot_cache["count"])
                    fcurve.keyframe_points.foreach_set('co', rot_cache["curves"][i])

        expression_cache = actor.cache["expressions"]
        viseme_cache = actor.cache["visemes"]
        for obj in objects:
            obj_action = utils.safe_get_action(obj.data.shape_keys)
            if obj_action:
                for expression_name in expression_cache:
                    if expression_name in obj.data.shape_keys.key_blocks:
                        key_cache = expression_cache[expression_name]
                        key = obj.data.shape_keys.key_blocks[expression_name]
                        data_path = key.path_from_id("value")
                        fcurve = obj_action.fcurves.new(data_path, action_group="Expression")
                        fcurve.keyframe_points.add(key_cache["count"])
                        fcurve.keyframe_points.foreach_set('co', key_cache["curves"][0])
                for viseme_name in viseme_cache:
                    if viseme_name in obj.data.shape_keys.key_blocks:
                        key_cache = viseme_cache[viseme_name]
                        key = obj.data.shape_keys.key_blocks[viseme_name]
                        data_path = key.path_from_id("value")
                        fcurve = obj_action.fcurves.new(data_path, action_group="Viseme")
                        fcurve.keyframe_points.add(key_cache["count"])
                        fcurve.keyframe_points.foreach_set('co', key_cache["curves"][0])

        actor.clear_cache()


class Signal():
    callbacks: list = None

    def __init__(self):
        self.callbacks = []

    def connect(self, func):
        self.callbacks.append(func)

    def disconnect(self, func=None):
        if func:
            self.callbacks.remove(func)
        else:
            self.callbacks.clear()

    def emit(self, *args):
        for func in self.callbacks:
            func(*args)


class LinkService():
    timer = None
    server_sock: socket.socket = None
    client_sock: socket.socket = None
    server_sockets = []
    client_sockets = []
    empty_sockets = []
    client_ip: str = "127.0.0.1"
    client_port: int = BLENDER_PORT
    is_listening: bool = False
    is_connected: bool = False
    is_connecting: bool = False
    ping_timer: float = 0
    keepalive_timer: float = 0
    time: float = 0
    is_data: bool = False
    is_sequence: bool = False
    # Signals
    listening = Signal()
    connecting = Signal()
    connected = Signal()
    lost_connection = Signal()
    server_stopped = Signal()
    client_stopped = Signal()
    received = Signal()
    accepted = Signal()
    sent = Signal()
    changed = Signal()
    sequence = Signal()
    # local props
    local_app: str = None
    local_version: str = None
    local_path: str = None
    # remote props
    remote_app: str = None
    remote_version: str = None
    remote_path: str = None
    remote_exe: str = None
    link_data: LinkData = None

    def __init__(self):
        global LINK_DATA
        self.link_data = LINK_DATA
        atexit.register(self.service_stop)

    def __enter__(self):
        return self

    def __exit__(self):
        self.service_stop()

    def is_cc(self):
        return self.remote_app == "Character Creator"

    def is_iclone(self):
        return self.remote_app == "iClone"

    def start_server(self):
        if not self.server_sock:
            try:
                self.keepalive_timer = HANDSHAKE_TIMEOUT_S
                self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.server_sock.settimeout(SOCKET_TIMEOUT)
                self.server_sock.bind(('', BLENDER_PORT))
                self.server_sock.listen(5)
                #self.server_sock.setblocking(False)
                self.server_sockets = [self.server_sock]
                self.is_listening = True
                utils.log_info(f"Listening on TCP *:{BLENDER_PORT}")
                self.listening.emit()
                self.changed.emit()
            except Exception as e:
                self.server_sock = None
                self.server_sockets = []
                self.is_listening = True
                utils.log_error(f"Unable to start server on TCP *:{BLENDER_PORT}", e)

    def stop_server(self):
        if self.server_sock:
            utils.log_info(f"Closing Server Socket")
            try:
                self.server_sock.shutdown()
                self.server_sock.close()
            except:
                pass
        self.is_listening = False
        self.server_sock = None
        self.server_sockets = []
        self.server_stopped.emit()
        self.changed.emit()

    def start_timer(self):
        self.time = time.time()
        if not self.timer:
            bpy.app.timers.register(self.loop, first_interval=TIMER_INTERVAL)
            self.timer = True
            utils.log_info(f"Service timer started")

    def stop_timer(self):
        if self.timer:
            try:
                bpy.app.timers.unregister(self.loop)
            except:
                pass
            self.timer = False
            utils.log_info(f"Service timer stopped")

    def try_start_client(self, host, port):
        if not self.client_sock:
            utils.log_info(f"Attempting to connect")
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(SOCKET_TIMEOUT)
                sock.connect((host, port))
                #sock.setblocking(False)
                self.is_connected = False
                self.is_connecting = True
                self.client_sock = sock
                self.client_sockets = [sock]
                self.client_ip = host
                self.client_port = port
                self.keepalive_timer = KEEPALIVE_TIMEOUT_S
                self.ping_timer = PING_INTERVAL_S
                utils.log_info(f"connecting with data link server on {host}:{port}")
                self.send_hello()
                self.connecting.emit()
                self.changed.emit()
                return True
            except:
                self.client_sock = None
                self.client_sockets = []
                self.is_connected = False
                self.is_connecting = False
                utils.log_info(f"Client socket connect failed!")
                return False
        else:
            utils.log_info(f"Client already connected!")
            return True

    def send_hello(self):
        self.local_app = "Blender"
        self.local_version = bpy.app.version_string
        self.local_path = get_link_data_path()
        json_data = {
            "Application": self.local_app,
            "Version": self.local_version,
            "Path": self.local_path
        }
        self.send(OpCodes.HELLO, encode_from_json(json_data))

    def stop_client(self):
        if self.client_sock:
            utils.log_info(f"Closing Client Socket")
            try:
                self.client_sock.shutdown()
                self.client_sock.close()
            except:
                pass
        self.is_connected = False
        self.is_connecting = False
        self.client_sock = None
        self.client_sockets = []
        if self.listening:
            self.keepalive_timer = HANDSHAKE_TIMEOUT_S
        self.client_stopped.emit()
        self.changed.emit()

    def has_client_sock(self):
        if self.client_sock and (self.is_connected or self.is_connecting):
            return True
        else:
            return False

    def recv(self):
        self.is_data = False
        if self.has_client_sock():
            try:
                r,w,x = select.select(self.client_sockets, self.empty_sockets, self.empty_sockets, 0)
            except Exception as e:
                utils.log_error("Client socket receive:select failed!", e)
                self.service_lost()
            count = 0
            while r:
                op_code = None
                try:
                    header = self.client_sock.recv(8)
                except Exception as e:
                    utils.log_error("Client socket receive:recv failed!", e)
                    self.service_lost()
                if header == 0:
                    print("Socket closed by client")
                    self.service_lost()
                    return
                if header and len(header) == 8:
                    op_code, size = struct.unpack("!II", header)
                    data = None
                    if size > 0:
                        data = bytearray()
                        while size > 0:
                            chunk_size = min(size, MAX_CHUNK_SIZE)
                            try:
                                chunk = self.client_sock.recv(chunk_size)
                            except Exception as e:
                                utils.log_error("Client socket receive:chunk_recv failed!", e)
                                self.service_lost()
                            data.extend(chunk)
                            size -= len(chunk)
                    self.parse(op_code, data)
                    self.received.emit(op_code, data)
                    count += 1
                self.is_data = False
                # parse may have received a disconnect notice
                if not self.has_client_sock():
                    return
                try:
                    r,w,x = select.select(self.client_sockets, self.empty_sockets, self.empty_sockets, 0)
                except Exception as e:
                    utils.log_error("Client socket receive:re-select failed!", e)
                    self.service_lost()
                if r:
                    self.is_data = True
                    if count >= MAX_RECEIVE or op_code == OpCodes.NOTIFY:
                        return

    def accept(self):
        if self.server_sock and self.is_listening:
            r,w,x = select.select(self.server_sockets, self.empty_sockets, self.empty_sockets, 0)
            while r:
                try:
                    sock, address = self.server_sock.accept()
                except Exception as e:
                    utils.log_error("Server socket accept failed!", e)
                    self.service_lost()
                    return
                self.client_sock = sock
                self.client_sockets = [sock]
                self.client_ip = address[0]
                self.client_port = address[1]
                self.is_connected = False
                self.is_connecting = True
                self.keepalive_timer = KEEPALIVE_TIMEOUT_S
                self.ping_timer = PING_INTERVAL_S
                utils.log_info(f"Incoming connection received from: {address[0]}:{address[1]}")
                self.send_hello()
                self.accepted.emit(self.client_ip, self.client_port)
                self.changed.emit()
                r,w,x = select.select(self.server_sockets, self.empty_sockets, self.empty_sockets, 0)

    def parse(self, op_code, data):
        self.keepalive_timer = KEEPALIVE_TIMEOUT_S

        if op_code == OpCodes.HELLO:
            utils.log_info(f"Hello Received")
            self.service_initialize()
            if data:
                json_data = decode_to_json(data)
                self.remote_app = json_data["Application"]
                self.remote_version = json_data["Version"]
                self.remote_path = json_data["Path"]
                self.remote_exe = json_data["Exe"]
                utils.log_always(f"Connected to: {self.remote_app} {self.remote_version}")
                utils.log_always(f"Using file path: {self.remote_path}")
                utils.log_always(f"Using exe path: {self.remote_exe}")

        elif op_code == OpCodes.PING:
            utils.log_info(f"Ping Received")

        elif op_code == OpCodes.STOP:
            utils.log_info(f"Termination Received")
            self.service_stop()

        elif op_code == OpCodes.DISCONNECT:
            utils.log_info(f"Disconnection Received")
            self.service_disconnect()

        elif op_code == OpCodes.NOTIFY:
            self.receive_notify(data)

        ##
        #

        elif op_code == OpCodes.TEMPLATE:
            self.receive_character_template(data)

        elif op_code == OpCodes.POSE:
            self.receive_pose(data)

        elif op_code == OpCodes.POSE_FRAME:
            self.receive_pose_frame(data)

        elif op_code == OpCodes.MORPH:
            self.receive_morph(data)

        elif op_code == OpCodes.MORPH_UPDATE:
            self.receive_morph(data, update=True)

        elif op_code == OpCodes.CHARACTER:
            self.receive_character_import(data)

        elif op_code == OpCodes.PROP:
            self.receive_character_import(data)

        elif op_code == OpCodes.CHARACTER_UPDATE:
            self.receive_character_update(data)

        elif op_code == OpCodes.RIGIFY:
            self.receive_rigify_request(data)

        elif op_code == OpCodes.SEQUENCE:
            self.receive_sequence(data)

        elif op_code == OpCodes.SEQUENCE_FRAME:
            self.receive_sequence_frame(data)

        elif op_code == OpCodes.SEQUENCE_END:
            self.receive_sequence_end(data)

        elif op_code == OpCodes.LIGHTS:
            self.receive_lights(data)

        elif op_code == OpCodes.CAMERA_SYNC:
            self.receive_camera_sync(data)

    def service_start(self, host, port):
        if not self.is_listening:
            self.start_timer()
            if SERVER_ONLY:
                self.start_server()
            else:
                if not self.try_start_client(host, port):
                    if not CLIENT_ONLY:
                        self.start_server()

    def service_initialize(self):
        if self.is_connecting:
            self.is_connecting = False
            self.is_connected = True
            self.on_connected()
            self.connected.emit()
            self.changed.emit()

    def service_disconnect(self):
        self.send(OpCodes.DISCONNECT)
        if CLIENT_ONLY:
            self.stop_timer()
        self.stop_client()

    def service_stop(self):
        self.send(OpCodes.STOP)
        self.stop_timer()
        self.stop_client()
        self.stop_server()

    def service_lost(self):
        self.lost_connection.emit()
        self.stop_timer()
        self.stop_client()
        self.stop_server()

    def check_service(self):
        global LINK_SERVICE
        global LINK_DATA
        if not LINK_SERVICE or not LINK_DATA:
            utils.log_error("Datalink service data lost. Due to script reload?")
            utils.log_error("Connection is maintained but actor data has been reset.")
            LINK_SERVICE = self
            LINK_DATA = self.link_data
            LINK_DATA.reset()
        return True

    def loop(self):
        current_time = time.time()
        delta_time = current_time - self.time
        self.time = current_time

        if not self.check_service():
            return None

        if not self.timer:
            return None

        if self.is_connected:
            self.ping_timer -= delta_time
            self.keepalive_timer -= delta_time

            if USE_PING and self.ping_timer <= 0:
                self.send(OpCodes.PING)

            if USE_KEEPALIVE and self.keepalive_timer <= 0:
                utils.log_info("lost connection!")
                self.service_stop()
                return None

        elif self.is_listening:
            self.keepalive_timer -= delta_time

            if USE_KEEPALIVE and self.keepalive_timer <= 0:
                utils.log_info("no connection within time limit!")
                self.service_stop()
                return None

        # accept incoming connections
        self.accept()

        # receive client data
        self.recv()

        # run anything in sequence
        self.sequence.emit()

        return 0.0 if (self.is_data or self.is_sequence) else TIMER_INTERVAL

    def send(self, op_code, binary_data = None):
        if self.client_sock and (self.is_connected or self.is_connecting):
            try:
                data_length = len(binary_data) if binary_data else 0
                header = struct.pack("!II", op_code, data_length)
                data = bytearray()
                data.extend(header)
                if binary_data:
                    data.extend(binary_data)
                self.client_sock.sendall(data)
                self.ping_timer = PING_INTERVAL_S
                self.sent.emit()
            except Exception as e:
                utils.log_error("Client socket send failed!", e)
                self.service_lost()

    def start_sequence(self, func=None):
        self.is_sequence = True
        if func:
            self.sequence.connect(func)
        else:
            self.sequence.disconnect()

    def stop_sequence(self):
        self.is_sequence = False
        self.sequence.disconnect()

    def on_connected(self):
        self.send_notify("Connected")

    def send_notify(self, message):
        global LINK_SERVICE
        notify_json = { "message": message }
        LINK_SERVICE.send(OpCodes.NOTIFY, encode_from_json(notify_json))

    def receive_notify(self, data):
        notify_json = decode_to_json(data)
        update_link_status(notify_json["message"])

    def get_remote_export_path(self, name):
        global LINK_SERVICE
        remote_path = LINK_SERVICE.remote_path
        local_path = LINK_SERVICE.local_path
        if remote_path:
            export_folder = remote_path
        else:
            export_folder = local_path
        return os.path.join(export_folder, name)

    def get_selected_actors(self):
        global LINK_DATA
        props = bpy.context.scene.CC3ImportProps

        selected_objects = bpy.context.selected_objects
        avatars = props.get_avatars()
        actors = []

        if not selected_objects and len(avatars) == 1:
            chr_cache = avatars[0]
            link_id = chr_cache.link_id
            actor = LINK_DATA.get_actor(link_id)
            if actor and actor not in actors:
                actors.append(actor)
        else:
            for obj in selected_objects:
                chr_cache = props.get_character_cache(obj, None)
                if chr_cache:
                    link_id = chr_cache.link_id
                    actor = LINK_DATA.get_actor(link_id)
                    if actor and actor not in actors:
                        actors.append(actor)
        return actors

    def get_active_actor(self):
        global LINK_DATA
        props = bpy.context.scene.CC3ImportProps
        active_object = utils.get_active_object()
        if active_object:
            chr_cache = props.get_character_cache(active_object, None)
            if chr_cache:
                link_id = chr_cache.link_id
                actor = LINK_DATA.get_actor(link_id)
                return actor
        return None

    def send_actor(self):
        global LINK_SERVICE
        actors = self.get_selected_actors()
        actor: LinkActor
        for actor in actors:
            self.send_notify(f"Blender Exporting: {actor.name}...")
            export_path = self.get_remote_export_path(actor.name + ".fbx")
            print(export_path)
            self.send_notify(f"Exporting: {actor.name}")
            bpy.ops.cc3.exporter(param="EXPORT_CC3", filepath=export_path)
            update_link_status(f"Sending: {actor.name}")
            export_data = encode_from_json({
                "path": export_path,
                "name": actor.name,
                "link_id": actor.link_id,
            })
            LINK_SERVICE.send(OpCodes.CHARACTER, export_data)
            update_link_status(f"Sent: {actor.name}")

    def send_morph(self):
        actors = self.get_selected_actors()
        actor: LinkActor
        for actor in actors:
            self.send_notify(f"Blender Exporting: {actor.name}...")
            export_path = self.get_remote_export_path(actor.name + "_morph.obj")
            key_path = self.get_remote_export_path(actor.name + "_morph.ObjKey")
            print(export_path)
            self.send_notify(f"Exporting: {actor.name}")
            bpy.ops.cc3.exporter(param="EXPORT_CC3", filepath=export_path)
            update_link_status(f"Sending: {actor.name}")
            export_data = encode_from_json({
                "path": export_path,
                "key_path": key_path,
                "name": actor.name,
                "link_id": actor.link_id,
                "morph_name": "Test Morph",
                "morph_path": "Some/Path",
            })
            LINK_SERVICE.send(OpCodes.MORPH, export_data)
            update_link_status(f"Sent: {actor.name}")

    def encode_character_templates(self, actors: list):
        pose_bone: bpy.types.PoseBone
        actor_data = []
        character_template = {
            "count": len(actors),
            "actors": actor_data
        }

        actor: LinkActor
        for actor in actors:
            chr_cache = actor.get_chr_cache()
            bones = []

            if chr_cache.rigified:
                # add the export retarget rig
                if utils.object_exists_is_armature(chr_cache.rig_export_rig):
                    export_rig = chr_cache.rig_export_rig
                else:
                    export_rig = rigging.adv_export_pair_rigs(chr_cache, link_target=True)[0]
                # get all the exportable deformation bones
                if utils.object_mode_to(export_rig):
                    for pose_bone in export_rig.pose.bones:
                        if pose_bone.name != "root" and not pose_bone.name.startswith("DEF-"):
                            bones.append(pose_bone.name)
            else:
                # get all the bones
                rig: bpy.types.Object = chr_cache.get_armature()
                if utils.object_mode_to(rig):
                    for pose_bone in rig.pose.bones:
                        bones.append(pose_bone.name)

            actor_data.append({
                "name": chr_cache.character_name,
                "link_id": chr_cache.link_id,
                "bones": bones
            })

        return encode_from_json(character_template)

    def encode_pose_data(self, actors: list):
        pose_bone: bpy.types.PoseBone
        data = bytearray()
        data += struct.pack("!II", len(actors), bpy.context.scene.frame_current)
        actor: LinkActor
        for actor in actors:
            data += pack_string(actor.name)
            data += pack_string(actor.link_id)
            chr_cache = actor.get_chr_cache()
            if chr_cache.rigified:
                # add the import retarget rig
                if utils.object_exists_is_armature(chr_cache.rig_export_rig):
                    export_rig = chr_cache.rig_export_rig
                else:
                    export_rig = rigging.adv_export_pair_rigs(chr_cache, link_target=True)[0]
                # pack all the bone data for the exportable deformation bones
                M: Matrix = export_rig.matrix_world
                if utils.object_mode_to(export_rig):
                    for pose_bone in export_rig.pose.bones:
                        if pose_bone.name != "root" and not pose_bone.name.startswith("DEF-"):
                            T: Matrix = M @ pose_bone.matrix
                            t = T.to_translation() * 100
                            r = T.to_quaternion()
                            s = T.to_scale()
                            data += struct.pack("!ffffffffff", t.x, t.y, t.z, r.x, r.y, r.z, r.w, s.x, s.y, s.z)
            else:
                # pack all the bone data
                rig: bpy.types.Object = chr_cache.get_armature()
                M: Matrix = rig.matrix_world
                if utils.object_mode_to(rig):
                    pose_bone: bpy.types.PoseBone
                    for pose_bone in rig.pose.bones:
                        T: Matrix = M @ pose_bone.matrix
                        t = T.to_translation()
                        r = T.to_quaternion()
                        s = T.to_scale()
                        data += struct.pack("!ffffffffff", t.x, t.y, t.z, r.x, r.y, r.z, r.w, s.x, s.y, s.z)
        return data

    def encode_sequence_data(self, actors):
        fps = bpy.context.scene.render.fps
        start_frame = bpy.context.scene.frame_start
        end_frame = bpy.context.scene.frame_end
        start_time = start_frame / fps
        end_time = end_frame / fps
        actors_data = []
        data = {
            "fps": fps,
            "start_time": start_time,
            "end_time": end_time,
            "start_frame": start_frame,
            "end_frame": end_frame,
            "actors": actors_data,
        }
        actor: LinkActor
        for actor in actors:
            actors_data.append({
                "name": actor.name,
                "link_id": actor.link_id,
            })
        return encode_from_json(data)

    def send_pose(self):
        global LINK_SERVICE
        # get actors
        actors = self.get_selected_actors()
        if actors:
            mode_selection = utils.store_mode_selection_state()
            update_link_status(f"Sending Current Pose Set")
            self.send_notify(f"Pose Set")
            # send template data first
            template_data = self.encode_character_templates(actors)
            LINK_SERVICE.send(OpCodes.TEMPLATE, template_data)
            # send pose data
            pose_data = self.encode_pose_data(actors)
            LINK_SERVICE.send(OpCodes.POSE, pose_data)
            utils.restore_mode_selection_state(mode_selection)

    def send_animation(self):
        return

    def send_sequence(self):
        global LINK_SERVICE
        global LINK_DATA

        # get actors
        actors = self.get_selected_actors()
        if actors:
            update_link_status(f"Sending Animation Sequence")
            self.send_notify(f"Animation Sequence")
            # reset animation to start
            bpy.context.scene.frame_current = bpy.context.scene.frame_start
            LINK_DATA.sequence_current_frame = bpy.context.scene.frame_current
            # send animation meta data
            sequence_data = self.encode_sequence_data(actors)
            LINK_SERVICE.send(OpCodes.SEQUENCE, sequence_data)
            # send template data first
            template_data = self.encode_character_templates(actors)
            LINK_SERVICE.send(OpCodes.TEMPLATE, template_data)
            # start the sending sequence
            LINK_DATA.sequence_actors = actors
            LINK_SERVICE.start_sequence(self.send_sequence_frame)

    def send_sequence_frame(self):
        global LINK_SERVICE
        global LINK_DATA

        # set/fetch the current frame in the sequence
        current_frame = ensure_current_frame(LINK_DATA.sequence_current_frame)
        update_link_status(f"Sequence Frame: {current_frame}")
        # send current sequence frame pose
        pose_data = self.encode_pose_data(LINK_DATA.sequence_actors)
        LINK_SERVICE.send(OpCodes.SEQUENCE_FRAME, pose_data)
        # check for end
        if current_frame >= bpy.context.scene.frame_end:
            LINK_DATA.sequence_actors = None
            LINK_SERVICE.stop_sequence()
            self.send_sequence_end()
            return
        # advance to next frame now
        LINK_DATA.sequence_current_frame = next_frame(current_frame)

    def send_sequence_end(self):
        LINK_SERVICE.send(OpCodes.SEQUENCE_END)

    def decode_character_templates(self, template_data):
        global LINK_DATA

        template_json = decode_to_json(template_data)
        count = template_json["count"]
        for actor_data in template_json["actors"]:
            link_id = actor_data["link_id"]
            name = actor_data["name"]
            actor = LINK_DATA.get_actor(link_id)
            if actor:
                actor.set_template(actor_data["bones"],
                                   actor_data["expressions"],
                                   actor_data["visemes"],
                                   actor_data["morphs"])
            else:
                utils.log_error(f"Unable to find actor: {name} ({link_id})")
        return template_json

    def decode_pose_frame(self, pose_data):
        count, frame = struct.unpack_from("!II", pose_data)
        return frame

    def decode_pose_data(self, pose_data):
        global LINK_DATA
        count, frame = struct.unpack_from("!II", pose_data)
        ensure_current_frame(frame)
        LINK_DATA.sequence_current_frame = frame
        offset = 8
        actors = []
        for i in range(0, count):
            offset, name = unpack_string(pose_data, offset)
            offset, link_id = unpack_string(pose_data, offset)
            actor = LINK_DATA.get_actor(link_id)
            objects = actor.get_sequence_objects()

            # unpack object transform
            tx,ty,tz,rx,ry,rz,rw,sx,sy,sz = struct.unpack_from("!ffffffffff", pose_data, offset)
            offset += 40
            rig: bpy.types.Object = actor.get_armature()
            if rig:
                loc = Vector((tx, ty, tz)) * 0.01
                rot = Quaternion((rw, rx, ry, rz))
                sca = Vector((sx, sy, sz))
                rig.location = loc
                rig.rotation_mode = "QUATERNION"
                rig.rotation_quaternion = rot
                for child in rig.children:
                    rig.location = (0,0,0)
                    rig.rotation_mode = "QUATERNION"
                    rig.rotation_quaternion = (1,0,0,0)
                #arm.scale = sca

            # unpack bone transforms
            num_bones = struct.unpack_from("!I", pose_data)[0]
            offset += 4
            if actor:
                actors.append(actor)
            else:
                utils.log_error(f"Unable to find actor: {name} ({link_id})")
                offset += num_bones * 40
                continue
            datalink_rig = make_datalink_import_rig(actor.chr_cache, actor.bones)

            # unpack the binary transform data directly into the datalink rig pose bones
            for bone_name in actor.bones:
                tx,ty,tz,rx,ry,rz,rw,sx,sy,sz = struct.unpack_from("!ffffffffff", pose_data, offset)
                offset += 40
                if actor:
                    pose_bone: bpy.types.PoseBone = datalink_rig.pose.bones[bone_name]
                    loc = Vector((tx, ty, tz)) * 0.01
                    rot = Quaternion((rw, rx, ry, rz))
                    sca = Vector((sx, sy, sz))
                    pose_bone.rotation_mode = "QUATERNION"
                    pose_bone.rotation_quaternion = rot
                    pose_bone.location = loc
                    pose_bone.scale = sca

            # unpack the expression shape keys into the mesh objects
            expression_weights = [0] * len(actor.expressions)
            for i, expression_name in enumerate(actor.expressions):
                weight = struct.unpack_from("!f", pose_data, offset)[0]
                offset += 4
                if actor and rig:
                    set_actor_expression_weight(objects, expression_name, weight)
                expression_weights[i] = weight

            # unpack the viseme shape keys into the mesh objects
            viseme_weights = [0] * len(actor.visemes)
            for i, viseme_name in enumerate(actor.visemes):
                weight = struct.unpack_from("!f", pose_data, offset)[0]
                offset += 4
                if actor and rig:
                    set_actor_viseme_weight(objects, expression_name, weight)
                viseme_weights[i] = weight

            # TODO: morph weights
            morph_weights = []

            # store shape keys in the cache
            store_shape_key_cache_keyframes(actor, frame, expression_weights, viseme_weights, morph_weights)

        return actors

    def find_link_id(self, link_id: str):
        for obj in bpy.data.objects:
            if "link_id" in obj and obj["link_id"] == link_id:
                return obj
        return None

    def add_spot_light(self, name):
        bpy.ops.object.light_add(type="SPOT")
        light = bpy.context.active_object
        light.name = name
        return light

    def add_area_light(self, name):
        bpy.ops.object.light_add(type="AREA")
        light = bpy.context.active_object
        light.name = name
        return light

    def add_point_light(self, name):
        bpy.ops.object.light_add(type="POINT")
        light = bpy.context.active_object
        light.name = name
        return light

    def add_dir_light(self, name):
        bpy.ops.object.light_add(type="SUN")
        light = bpy.context.active_object
        light.name = name
        return light

    def decode_lights_data(self, data):
        lights_data = decode_to_json(data)

        RECTANGULAR_SPOTLIGHTS_AS_AREA = False

        for light_data in lights_data["lights"]:

            is_dir = light_data["type"] == "DIR"
            is_point = light_data["type"] == "POINT" and light_data["is_rectangle"] == False and light_data["is_tube"] == False
            if RECTANGULAR_SPOTLIGHTS_AS_AREA:
                is_area = ((light_data["type"] == "POINT" and (light_data["is_rectangle"] == True or light_data["is_tube"] == True))
                        or (light_data["type"] == "SPOT" and light_data["is_rectangle"] == True))
                is_spot = light_data["type"] == "SPOT" and light_data["is_rectangle"] == False
            else:
                is_area = (light_data["type"] == "POINT" and (light_data["is_rectangle"] == True or light_data["is_tube"] == True))
                is_spot = light_data["type"] == "SPOT"

            light_type = "SUN" if is_dir else "AREA" if is_area else "POINT" if is_point else "SPOT"

            light = self.find_link_id(light_data["link_id"])
            if light and (light.type != "LIGHT" or light.data.type != light_type):
                utils.delete_light_object(light)
                light = None
            if not light:
                if is_area:
                    light = self.add_area_light(light_data["name"])
                elif is_point:
                    light = self.add_point_light(light_data["name"])
                elif is_dir:
                    light = self.add_dir_light(light_data["name"])
                else:
                    light = self.add_spot_light(light_data["name"])
                light["link_id"] = light_data["link_id"]

            light.location = utils.array_to_vector(light_data["loc"]) / 100
            light.rotation_mode = "QUATERNION"
            light.rotation_quaternion = utils.array_to_quaternion(light_data["rot"])
            light.scale = utils.array_to_vector(light_data["sca"])
            light.data.color = utils.array_to_color(light_data["color"], False)
            mult = light_data["multiplier"]
            #if mult > 10:
            #    mult *= (1 + pow((mult - 10)/90, 1.5))
            if is_dir:
                light.data.energy = 450 * pow(light_data["multiplier"]/20, 2)
            elif is_spot:
                light.data.energy = 40.0 * mult
            elif is_point:
                light.data.energy = 40.0 * mult
            elif is_area:
                light.data.energy = 10.0 * mult
            if not is_dir:
                light.data.use_custom_distance = True
                light.data.cutoff_distance = light_data["range"] / 100
            if is_spot:
                light.data.spot_size = light_data["angle"] * 0.01745329
                light.data.spot_blend = 0.01 * light_data["falloff"] * (0.5 + 0.01 * light_data["attenuation"] * 0.5)
                if light_data["is_rectangle"]:
                    light.data.shadow_soft_size = (light_data["rect"][0] + light_data["rect"][0]) / 200
                elif light_data["is_tube"]:
                    light.data.shadow_soft_size = light_data["tube_radius"] / 100
            if is_area:
                if light_data["is_rectangle"]:
                    light.data.shape = "RECTANGLE"
                    light.data.size = light_data["rect"][0] / 100
                    light.data.size_y = light_data["rect"][1] / 100
                elif light_data["is_tube"]:
                    light.data.shape = "DISK"
                    light.data.size = light_data["tube_radius"] / 100
            light.data.use_shadow = light_data["cast_shadow"]
            if light_data["cast_shadow"]:
                if not is_dir:
                    light.data.shadow_buffer_clip_start = 0.0025
                    light.data.shadow_buffer_bias = 1.0
                light.data.use_contact_shadow = True
                light.data.contact_shadow_distance = 0.05
                light.data.contact_shadow_thickness = 0.0025
            light.hide_set(not light_data["active"])

        # clean up lights not found in scene
        for obj in bpy.data.objects:
            if obj.type == "LIGHT":
               if "link_id" in obj and obj["link_id"] not in lights_data["scene_lights"]:
                   utils.delete_light_object(obj)
        #
        bpy.context.scene.eevee.use_gtao = True
        bpy.context.scene.eevee.gtao_distance = 0.25
        bpy.context.scene.eevee.gtao_factor = 0.5
        bpy.context.scene.eevee.use_bloom = True
        bpy.context.scene.eevee.bloom_threshold = 0.8
        bpy.context.scene.eevee.bloom_knee = 0.5
        bpy.context.scene.eevee.bloom_radius = 2.0
        bpy.context.scene.eevee.bloom_intensity = 1.0
        bpy.context.scene.eevee.use_ssr = True
        bpy.context.scene.eevee.use_ssr_refraction = True
        bpy.context.scene.eevee.bokeh_max_size = 32
        colorspace.set_view_settings("Filmic", "High Contrast", 0, 0.75)
        if bpy.context.scene.cycles.transparent_max_bounces < 50:
            bpy.context.scene.cycles.transparent_max_bounces = 50
        view_space: bpy.types.Area = self.get_view_space()
        if view_space:
            view_space.shading.type = 'MATERIAL'
            view_space.shading.use_scene_lights = True
            view_space.shading.use_scene_world = False
            view_space.shading.studio_light = 'studio.exr'
            view_space.shading.studiolight_rotate_z = 0.0 * 0.01745329
            view_space.shading.studiolight_intensity = 0.2
            view_space.shading.studiolight_background_alpha = 0.0
            view_space.shading.studiolight_background_blur = 0.5
            if LINK_SERVICE.is_cc():
                # only hide the lights if it's from Character Creator
                view_space.overlay.show_extras = False

    def receive_lights(self, data):
        update_link_status(f"Light Data Receveived")
        self.decode_lights_data(data)


    # Camera
    #

    def get_region_3d(self):
        space = self.get_view_space()
        if space:
            return space, space.region_3d
        return None, None

    def get_view_space(self):
        area = self.get_view_area()
        if area:
            return area.spaces.active
        return None

    def get_view_area(self):
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                return area
        return None

    def get_view_camera_data(self):
        view_space: bpy.types.Space
        r3d: bpy.types.RegionView3D
        view_space, r3d = self.get_region_3d()
        t = r3d.view_location
        r = r3d.view_rotation
        d = r3d.view_distance
        dir = Vector((0,0,-1))
        dir.rotate(r)
        loc: Vector = t - (dir * d)
        lens = view_space.lens
        data = {
            "link_id": "0",
            "name": "Viewport Camera",
            "loc": [loc.x, loc.y, loc.z],
            "rot": [r.x, r.y, r.z, r.w],
            "sca": [1, 1, 1],
            "focal_length": lens,
        }
        return data

    def get_view_camera_pivot(self):
        view_space, r3d = self.get_region_3d()
        t = r3d.view_location
        return t

    def send_camera_sync(self):
        global LINK_SERVICE
        #
        update_link_status(f"Synchronizing View Camera")
        self.send_notify(f"Sync View Camera")
        camera_data = self.get_view_camera_data()
        pivot = self.get_view_camera_pivot()
        data = {
            "view_camera": camera_data,
            "pivot": [pivot.x, pivot.y, pivot.z],
        }
        LINK_SERVICE.send(OpCodes.CAMERA_SYNC, encode_from_json(data))

    def decode_camera_sync_data(self, data):
        data = decode_to_json(data)
        camera_data = data["view_camera"]
        pivot = utils.array_to_vector(data["pivot"]) / 100
        view_space, r3d = self.get_region_3d()
        loc = utils.array_to_vector(camera_data["loc"]) / 100
        rot = utils.array_to_quaternion(camera_data["rot"])
        to_pivot = pivot - loc
        dir = Vector((0,0,-1))
        dir.rotate(rot)
        dist = to_pivot.dot(dir)
        if dist <= 0:
            dist = 1.0
        r3d.view_location = loc + dir * dist
        r3d.view_rotation = rot
        r3d.view_distance = dist
        view_space.lens = camera_data["focal_length"]

    def receive_camera_sync(self, data):
        update_link_status(f"Camera Data Receveived")
        self.decode_camera_sync_data(data)


    # Character Pose
    #

    def receive_character_template(self, data):
        self.decode_character_templates(data)
        update_link_status(f"Character Templates Received")

    def select_actor_rigs(self, actors, start_frame=0, end_frame=0):
        rigs = []
        actor: LinkActor
        for actor in actors:
            rig = actor.get_armature()
            if rig:
                rigs.append(rig)
        all_selected = True
        if not (utils.get_mode() == "POSE" and len(bpy.context.selected_objects) == len(rigs)):
            all_selected = False
        else:
            for rig in rigs:
                if rig not in bpy.context.selected_objects:
                    all_selected = False
                    break
        if not all_selected:
            utils.set_mode("OBJECT")
            utils.clear_selected_objects()
            utils.try_select_objects(rigs, True, make_active=True)
            utils.set_mode("POSE")
        return rigs

    def receive_pose(self, data):
        global LINK_SERVICE
        global LINK_DATA

        update_link_status(f"Receiving Pose...")
        json_data = decode_to_json(data)
        # pose frame
        LINK_DATA.pose_frame = json_data["frame"]
        # pose actors
        actors_data = json_data["actors"]
        actors = []
        for actor_data in actors_data:
            name = actor_data["name"]
            link_id = actor_data["link_id"]
            actor = LINK_DATA.get_actor(link_id)
            if actor:
                prep_rig(actor, json_data["frame"], json_data["frame"])
                actors.append(actor)
        LINK_DATA.pose_actors = actors
        set_frame(LINK_DATA.pose_frame)

    def receive_pose_frame(self, data):
        global LINK_DATA
        # decode and cache pose
        frame = self.decode_pose_frame(data)
        actors = self.decode_pose_data(data)
        update_link_status(f"Pose Frame: {frame}")
        # store bone pose
        #set_frame(frame)
        bpy.context.view_layer.update()
        rigs = self.select_actor_rigs(actors)
        for actor in actors:
            store_bone_cache_keyframes(actor, frame)
        # write pose action
        for actor in actors:
            remove_datalink_import_rig(actor)
            write_sequence_actions(actor)
        # finish
        LINK_DATA.pose_actors = None
        bpy.context.scene.frame_current = frame

    def receive_sequence(self, data):
        global LINK_SERVICE
        global LINK_DATA

        update_link_status(f"Receiving Live Sequence...")
        json_data = decode_to_json(data)
        # sequence frame range
        LINK_DATA.sequence_start_frame = json_data["start_frame"]
        LINK_DATA.sequence_end_frame = json_data["end_frame"]
        # sequence actors
        actors_data = json_data["actors"]
        actors = []
        for actor_data in actors_data:
            name = actor_data["name"]
            link_id = actor_data["link_id"]
            actor = LINK_DATA.get_actor(link_id)
            if actor:
                prep_rig(actor, json_data["start_frame"], json_data["end_frame"])
                actors.append(actor)
        LINK_DATA.sequence_actors = actors
        # set the range
        set_frame_range(LINK_DATA.sequence_start_frame, LINK_DATA.sequence_end_frame)
        set_frame(LINK_DATA.sequence_start_frame)
        # start the sequence
        LINK_SERVICE.start_sequence()

    def receive_sequence_frame(self, data):
        global LINK_SERVICE
        global LINK_DATA
        # decode and cache pose
        frame = self.decode_pose_frame(data)
        actors = self.decode_pose_data(data)
        update_link_status(f"Sequence Frame: {LINK_DATA.sequence_current_frame}")
        #set_frame(frame)
        bpy.context.view_layer.update()
        rigs = self.select_actor_rigs(actors)
        for actor in actors:
            store_bone_cache_keyframes(actor, frame)

    def receive_sequence_end(self, data):
        global LINK_SERVICE
        global LINK_DATA

        utils.log_info("SEQUENCE END")
        json_data = decode_to_json(data)
        actors_data = json_data["actors"]
        actors = []
        for actor_data in actors_data:
            name = actor_data["name"]
            link_id = actor_data["link_id"]
            actor = LINK_DATA.get_actor(link_id)
            if actor:
                actors.append(actor)
        num_frames = LINK_DATA.sequence_end_frame - LINK_DATA.sequence_start_frame + 1
        actor: LinkActor
        for actor in LINK_DATA.sequence_actors:
            remove_datalink_import_rig(actor)
            write_sequence_actions(actor)
        LINK_SERVICE.stop_sequence()
        LINK_DATA.sequence_actors = None
        update_link_status(f"Live Sequence Complete: {num_frames} frames")
        bpy.context.scene.frame_current = LINK_DATA.sequence_start_frame
        bpy.ops.screen.animation_play()

    def receive_character_import(self, data):
        global LINK_DATA

        json_data = decode_to_json(data)
        fbx_path = json_data["path"]
        name = json_data["name"]
        link_id = json_data["link_id"]
        actor = LINK_DATA.get_actor(link_id)
        print(fbx_path)
        print(name)
        print(link_id)
        print(os.path.exists(fbx_path))
        if actor:
            update_link_status(f"Character: {name} exists!")
            utils.log_error(f"Actor {name} ({link_id}) already exists!")
            return
        update_link_status(f"Receving Character Import: {name}")
        if os.path.exists(fbx_path):
            bpy.ops.cc3.importer(param="IMPORT", filepath=fbx_path, link_id=link_id)
            actor = LINK_DATA.get_actor(link_id)
            update_link_status(f"Character Imported: {actor.name}")

    def receive_character_update(self, data):
        global LINK_DATA
        json_data = decode_to_json(data)
        old_name = json_data["old_name"]
        new_name = json_data["new_name"]
        old_link_id = json_data["old_link_id"]
        new_link_id = json_data["new_link_id"]
        actor = LINK_DATA.get_actor(old_link_id)
        actor.update(new_name, new_link_id)

    def receive_morph(self, data, update=False):
        global LINK_DATA

        json_data = decode_to_json(data)
        obj_path = json_data["path"]
        name = json_data["name"]
        link_id = json_data["link_id"]
        actor = LINK_DATA.get_actor(link_id)
        if actor and not update:
            update_link_status(f"Character: {name} exists!")
            utils.log_error(f"Actor {name} ({link_id}) already exists!")
            return
        update_link_status(f"Receving Character Morph: {name}")
        if os.path.exists(obj_path):
            if update:
                self.import_update_morph(actor, obj_path)
            else:
                bpy.ops.cc3.importer(param="IMPORT", filepath=obj_path, link_id=link_id)
                actor = LINK_DATA.get_actor(link_id)
                update_link_status(f"Morph Imported: {actor.name}")

    def import_update_morph(self, actor: LinkActor, file_path):
        utils.tag_objects()
        importer.obj_import(file_path, split_objects=False, split_groups=False, vgroups=True)
        objects = utils.untagged_objects()
        if objects:
            for source in objects:
                source.scale = (0.01, 0.01, 0.01)
                dest = actor.chr_cache.object_cache[0].object
                geom.copy_vert_positions_by_index(source, dest)
                utils.delete_mesh_object(source)

    def receive_rigify_request(self, data):
        global LINK_SERVICE

        json_data = decode_to_json(data)
        name = json_data["name"]
        link_id = json_data["link_id"]
        actor = LINK_DATA.get_actor(link_id)
        if actor:
            update_link_status(f"Rigifying: {actor.name}")
            actor.chr_cache.select()
            bpy.ops.cc3.rigifier(param="ALL", no_face_rig=True)
            update_link_status(f"Character Rigified: {actor.name}")






LINK_SERVICE: LinkService = None


def get_link_service():
    global LINK_SERVICE
    return LINK_SERVICE


def link_state_update():
    global LINK_SERVICE
    if LINK_SERVICE:
        link_data = bpy.context.scene.CCICLinkData
        link_data.link_listening = LINK_SERVICE.is_listening
        link_data.link_connected = LINK_SERVICE.is_connected
        link_data.link_connecting = LINK_SERVICE.is_connecting
        utils.update_ui()


def update_link_status(text):
    link_data = bpy.context.scene.CCICLinkData
    link_data.link_status = text
    utils.update_ui()


class CCICDataLink(bpy.types.Operator):
    """Data Link Control Operator"""
    bl_idname = "ccic.datalink"
    bl_label = "Listener"
    bl_options = {"REGISTER"}

    param: bpy.props.StringProperty(
            name = "param",
            default = "",
            options={"HIDDEN"}
        )

    def execute(self, context):
        global LINK_SERVICE

        if self.param == "START":
            self.link_start()

        elif self.param == "DISCONNECT":
            self.link_disconnect()

        elif self.param == "STOP":
            self.link_stop()

        if LINK_SERVICE:

            if self.param == "SEND_POSE":
                LINK_SERVICE.send_pose()

            elif self.param == "SEND_ANIM":
                LINK_SERVICE.send_sequence()

            elif self.param == "SEND_ACTOR":
                LINK_SERVICE.send_actor()

            elif self.param == "SEND_MORPH":
                LINK_SERVICE.send_morph()

            elif self.param == "SYNC_CAMERA":
                LINK_SERVICE.send_camera_sync()

            elif self.param == "TEST":
                LINK_SERVICE.stop_client()

        return {'FINISHED'}

    def prep_local_files(self):
        data_path = get_link_data_path()
        if data_path:
            os.makedirs(data_path, exist_ok=True)

    def link_start(self, is_go_b=False):
        link_data = bpy.context.scene.CCICLinkData
        global LINK_SERVICE

        self.prep_local_files()
        if not LINK_SERVICE:
            LINK_SERVICE = LinkService()
            LINK_SERVICE.changed.connect(link_state_update)

        if LINK_SERVICE:
            LINK_SERVICE.service_start(link_data.link_host_ip, link_data.link_port)

    def link_stop(self):
        global LINK_SERVICE

        if LINK_SERVICE:
            LINK_SERVICE.service_stop()

    def link_disconnect(self):
        global LINK_SERVICE

        if LINK_SERVICE:
            LINK_SERVICE.service_disconnect()


