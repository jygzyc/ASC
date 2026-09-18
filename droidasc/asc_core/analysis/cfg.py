"""Control-flow graph construction straight from method bytecode.

Uses the ASC opcode table (oplen/format) for a linear sweep, resolves branch
targets from the raw operand encodings, splits basic blocks at leaders and
emits DOT or JSON. Payloads (packed/sparse switch, fill-array) are parsed so
switch case targets become real CFG edges.
"""

import json
import struct

from ..models.dvm_opcode import Format, IndexFlag, opcodes

_H = struct.Struct("<H").unpack
_h = struct.Struct("<h").unpack
_i = struct.Struct("<i").unpack
_I = struct.Struct("<I").unpack
_q = struct.Struct("<q").unpack

_GOTO = {0x28, 0x29, 0x2A}
_IF = set(range(0x32, 0x3E))          # if-* and if-*z
_SWITCH = {0x2B, 0x2C}
_FILL_ARRAY = {0x26}
_RET = {0x0E, 0x0F, 0x10, 0x11}
_THROW = {0x27}

_PACKED_SWITCH_IDENT = 0x0100
_SPARSE_SWITCH_IDENT = 0x0200
_ARRAY_PAYLOAD_IDENT = 0x0300


class Insn:
    __slots__ = ("offset", "op", "opcode", "size", "target", "payload_off",
                 "case_targets", "is_payload")

    def __init__(self, offset, op, opcode, size):
        self.offset = offset          # byte offset into the code item insns
        self.op = op                  # DvmOpcode or None for payloads
        self.opcode = opcode
        self.size = size              # size in bytes
        self.target = None            # single branch target (byte offset)
        self.payload_off = None       # payload location for switch/fill-array
        self.case_targets = None      # [(key, byte offset)] for switches
        self.is_payload = False


def _branch_target(bytecode, pc, op):
    fmt = op.fmt
    if fmt == Format.k10t:
        v = bytecode[pc + 1]
        if v >= 0x80:
            v -= 0x100
        return pc + v * 2
    if fmt in (Format.k20t, Format.k21t, Format.k22t):
        return pc + _h(bytes(bytecode[pc + 2:pc + 4]))[0] * 2
    if fmt in (Format.k30t, Format.k31t):
        return pc + _i(bytes(bytecode[pc + 2:pc + 6]))[0] * 2
    return None


def _payload_size(bytecode, off):
    """Size in bytes of a payload starting at byte offset off."""
    ident = _H(bytes(bytecode[off:off + 2]))[0]
    if ident == _PACKED_SWITCH_IDENT:
        size = _H(bytes(bytecode[off + 2:off + 4]))[0]
        return (4 + size * 2) * 2
    if ident == _SPARSE_SWITCH_IDENT:
        size = _H(bytes(bytecode[off + 2:off + 4]))[0]
        return (2 + size * 4) * 2
    if ident == _ARRAY_PAYLOAD_IDENT:
        width = _H(bytes(bytecode[off + 2:off + 4]))[0]
        size = _I(bytes(bytecode[off + 4:off + 8]))[0]
        return 8 + ((width * size + 1) & ~1)
    return None


def _parse_switch_payload(bytecode, switch_pc, payload_off):
    """Return [(key, absolute byte target)] for a switch payload."""
    if payload_off < 0 or payload_off + 4 > len(bytecode):
        return []
    ident = _H(bytes(bytecode[payload_off:payload_off + 2]))[0]
    size = _H(bytes(bytecode[payload_off + 2:payload_off + 4]))[0]
    targets = []
    if ident == _PACKED_SWITCH_IDENT:
        first_key = _i(bytes(bytecode[payload_off + 4:payload_off + 8]))[0]
        pos = payload_off + 8
        for i in range(size):
            rel = _i(bytes(bytecode[pos:pos + 4]))[0]
            targets.append((first_key + i, switch_pc + rel * 2))
            pos += 4
    elif ident == _SPARSE_SWITCH_IDENT:
        keys_pos = payload_off + 4
        tgts_pos = keys_pos + size * 4
        for i in range(size):
            key = _i(bytes(bytecode[keys_pos + i * 4:keys_pos + i * 4 + 4]))[0]
            rel = _i(bytes(bytecode[tgts_pos + i * 4:tgts_pos + i * 4 + 4]))[0]
            targets.append((key, switch_pc + rel * 2))
    return targets


def decode_instructions(bytecode):
    """Linear sweep over a code_item insn array (list of bytes).

    Returns a list of Insn in offset order; payloads appear as marked
    placeholder entries so block splitting never lands inside them.
    """
    insns = []
    pc = 0
    n = len(bytecode)
    while pc < n:
        op_byte = bytecode[pc]
        op = opcodes.get(op_byte)
        if op is None:
            break
        if op_byte == 0x00 and pc + 2 <= n:
            ident = _H(bytes(bytecode[pc:pc + 2]))[0]
            if ident in (_PACKED_SWITCH_IDENT, _SPARSE_SWITCH_IDENT, _ARRAY_PAYLOAD_IDENT):
                size = _payload_size(bytecode, pc)
                if size is not None and pc + size <= n:
                    insn = Insn(pc, None, op_byte, size)
                    insn.is_payload = True
                    insns.append(insn)
                    pc += size
                    continue
        size = op.oplen * 2
        if pc + size > n:
            break
        insn = Insn(pc, op, op_byte, size)
        if op_byte in _GOTO or op_byte in _IF:
            insn.target = _branch_target(bytecode, pc, op)
        elif op_byte in _SWITCH or op_byte in _FILL_ARRAY:
            insn.payload_off = _branch_target(bytecode, pc, op)
            if op_byte in _SWITCH and insn.payload_off is not None:
                insn.case_targets = _parse_switch_payload(bytecode, pc, insn.payload_off)
        insns.append(insn)
        pc += size
    return insns


class Block:
    __slots__ = ("id", "offset", "insns", "edges")

    def __init__(self, bid, offset):
        self.id = bid
        self.offset = offset
        self.insns = []
        self.edges = []               # [(target_block_id, kind, label)]


def build_blocks(insns):
    """Split a decoded instruction list into basic blocks with typed edges."""
    if not insns:
        return []
    offsets = {i.offset for i in insns}
    end = insns[-1].offset + insns[-1].size

    leaders = {insns[0].offset}
    for insn in insns:
        if insn.is_payload:
            continue
        nxt = insn.offset + insn.size
        is_branch = insn.opcode in _GOTO or insn.opcode in _IF or insn.opcode in _SWITCH
        is_term = insn.opcode in _RET or insn.opcode in _THROW
        if insn.target is not None and insn.target in offsets:
            leaders.add(insn.target)
        if insn.case_targets:
            for _key, tgt in insn.case_targets:
                if tgt in offsets:
                    leaders.add(tgt)
        if (is_branch or is_term) and nxt in offsets:
            leaders.add(nxt)

    blocks = []
    block_by_leader = {}
    current = None
    for insn in insns:
        if insn.offset in leaders or current is None:
            current = Block(len(blocks), insn.offset)
            blocks.append(current)
            block_by_leader[insn.offset] = current
        current.insns.append(insn)

    block_at = {}
    for b in blocks:
        for insn in b.insns:
            block_at[insn.offset] = b

    for b in blocks:
        last = None
        for insn in b.insns:
            if insn.is_payload:
                continue
            last = insn
        if last is None:
            # block that only holds payload bytes: chain to the next block
            if b.id + 1 < len(blocks):
                b.edges.append((b.id + 1, "unconditional", ""))
            continue
        nxt_off = last.offset + last.size
        fall = block_at.get(nxt_off) if nxt_off < end else None

        if last.opcode in _GOTO:
            tgt = block_at.get(last.target)
            if tgt is not None:
                b.edges.append((tgt.id, "unconditional", ""))
        elif last.opcode in _IF:
            tgt = block_at.get(last.target)
            if tgt is not None:
                b.edges.append((tgt.id, "conditional_true", "T"))
            if fall is not None:
                b.edges.append((fall.id, "conditional_false", "F"))
        elif last.opcode in _SWITCH:
            seen = set()
            for key, toff in (last.case_targets or []):
                tgt = block_at.get(toff)
                if tgt is not None and tgt.id not in seen:
                    seen.add(tgt.id)
                    b.edges.append((tgt.id, "switch", f"case {key}"))
            if fall is not None and fall.id not in seen:
                b.edges.append((fall.id, "default", "default"))
        elif last.opcode in _RET or last.opcode in _THROW:
            pass
        else:
            if fall is not None and fall is not b:
                b.edges.append((fall.id, "unconditional", ""))
    return blocks


# ---------------------------------------------------------------------------
# light disassembly (operand-aware, resolves indices through the DEX when given)

def _s8(v):
    return v - 0x100 if v >= 0x80 else v


def _s4(v):
    return v - 0x10 if v >= 0x8 else v


def _resolve_ref(dex, idx_flag, idx):
    if dex is None:
        return f"@{idx}"
    try:
        if idx_flag == IndexFlag.kIndexStringRef:
            text = str(dex.strings[idx]).replace("\\", "\\\\").replace('"', '\\"')
            text = text.replace("\n", "\\n")
            if len(text) > 48:
                text = text[:45] + "..."
            return f'"{text}"'
        if idx_flag == IndexFlag.kIndexTypeRef:
            return dex.types[idx].descriptor
        if idx_flag == IndexFlag.kIndexFieldRef:
            f = dex.fields[idx]
            return f"{f.cls.fullname}->{f.name}"
        if idx_flag in (IndexFlag.kIndexMethodRef, IndexFlag.kIndexMethodAndProtoRef):
            m = dex.methods[idx]
            proto = m.prototype
            ret = str(dex.types[proto.return_type_idx])
            params = "".join(str(p) for p in proto.parameters_type)
            return f"{m.cls.fullname}->{m.name}({params}){ret}"
        if idx_flag == IndexFlag.kIndexProtoRef:
            proto = dex.get_prototype(idx)
            ret = str(dex.types[proto.return_type_idx])
            params = "".join(str(p) for p in proto.parameters_type)
            return f"({params}){ret}"
    except (IndexError, KeyError, struct.error):
        pass
    return f"@{idx}"


def disassemble(insn, bytecode, dex=None):
    """Render one instruction as a smali-like line."""
    if insn.is_payload:
        return f"... payload ({insn.size // 2} units)"
    op = insn.op
    name = op.name
    fmt = op.fmt
    pc = insn.offset
    w0 = _H(bytes(bytecode[pc:pc + 2]))[0]
    a_hi = w0 >> 8

    if fmt == Format.k10x:
        return name
    if fmt == Format.k12x:
        return f"{name} v{a_hi & 0x0f}, v{w0 >> 12}"
    if fmt == Format.k11n:
        return f"{name} v{a_hi & 0x0f}, #{_s4(w0 >> 12)}"
    if fmt == Format.k11x:
        return f"{name} v{a_hi}"
    if fmt in (Format.k10t, Format.k20t, Format.k30t):
        return f"{name} :L{insn.target if insn.target is not None else '?'}"
    if fmt in (Format.k21t, Format.k22t):
        if fmt == Format.k21t:
            regs = f"v{a_hi}"
        else:
            regs = f"v{a_hi & 0x0f}, v{w0 >> 12}"
        return f"{name} {regs}, :L{insn.target if insn.target is not None else '?'}"
    if fmt == Format.k31t:
        extra = f" ; {len(insn.case_targets)} cases" if insn.case_targets else ""
        return f"{name} v{a_hi}, :L{insn.payload_off}{extra}"
    if fmt in (Format.k21c, Format.k31c):
        if fmt == Format.k21c:
            idx = _H(bytes(bytecode[pc + 2:pc + 4]))[0]
        else:
            idx = _I(bytes(bytecode[pc + 2:pc + 6]))[0]
        return f"{name} v{a_hi}, {_resolve_ref(dex, op.idx, idx)}"
    if fmt == Format.k22c:
        idx = _H(bytes(bytecode[pc + 2:pc + 4]))[0]
        return f"{name} v{a_hi & 0x0f}, v{w0 >> 12}, {_resolve_ref(dex, op.idx, idx)}"
    if fmt == Format.k21s:
        return f"{name} v{a_hi}, #{_h(bytes(bytecode[pc + 2:pc + 4]))[0]}"
    if fmt == Format.k21h:
        raw = _h(bytes(bytecode[pc + 2:pc + 4]))[0]
        bits = 48 if "wide" in name else 16
        return f"{name} v{a_hi}, #{raw << bits}"
    if fmt == Format.k22s:
        lit = _h(bytes(bytecode[pc + 2:pc + 4]))[0]
        return f"{name} v{a_hi & 0x0f}, v{w0 >> 12}, #{lit}"
    if fmt == Format.k22b:
        lit = bytecode[pc + 3]
        return f"{name} v{a_hi}, v{bytecode[pc + 2]}, #{_s8(lit)}"
    if fmt == Format.k31i:
        return f"{name} v{a_hi}, #{_i(bytes(bytecode[pc + 2:pc + 6]))[0]}"
    if fmt == Format.k51l:
        return f"{name} v{a_hi}, #{_q(bytes(bytecode[pc + 2:pc + 10]))[0]}"
    if fmt == Format.k23x:
        w1 = _H(bytes(bytecode[pc + 2:pc + 4]))[0]
        return f"{name} v{a_hi}, v{w1 & 0xff}, v{w1 >> 8}"
    if fmt == Format.k22x:
        return f"{name} v{a_hi}, v{_H(bytes(bytecode[pc + 2:pc + 4]))[0]}"
    if fmt == Format.k32x:
        w1 = _H(bytes(bytecode[pc + 2:pc + 4]))[0]
        w2 = _H(bytes(bytecode[pc + 4:pc + 6]))[0]
        return f"{name} v{w1}, v{w2}"
    if fmt == Format.k35c:
        idx = _H(bytes(bytecode[pc + 2:pc + 4]))[0]
        regs_w = _H(bytes(bytecode[pc + 4:pc + 6]))[0]
        count = a_hi >> 4
        regs = [regs_w & 0x0f, (regs_w >> 4) & 0x0f, (regs_w >> 8) & 0x0f,
                (regs_w >> 12) & 0x0f, a_hi & 0x0f]
        reg_list = ", ".join(f"v{r}" for r in regs[:count])
        return f"{name} {{{reg_list}}}, {_resolve_ref(dex, op.idx, idx)}"
    if fmt == Format.k3rc:
        idx = _H(bytes(bytecode[pc + 2:pc + 4]))[0]
        start = _H(bytes(bytecode[pc + 4:pc + 6]))[0]
        return f"{name} {{v{start} .. v{start + a_hi - 1}}}, {_resolve_ref(dex, op.idx, idx)}"
    if fmt in (Format.k45cc, Format.k4rcc):
        idx = _H(bytes(bytecode[pc + 2:pc + 4]))[0]
        return f"{name} ..., {_resolve_ref(dex, op.idx, idx)}"
    return name


# ---------------------------------------------------------------------------
# public API

def find_method(clazz, query: str):
    """Resolve 'name', 'name(params)' or 'name(params)ret' against a DexClass."""
    name = query
    proto_q = None
    if "(" in query:
        name = query[:query.index("(")]
        proto_q = query[query.index("("):]
    matches = []
    for m in clazz.methods:
        if m.name != name:
            continue
        if proto_q is not None:
            proto = m.prototype
            ret = str(clazz.dex.get_type(proto.return_type_idx))
            params = "".join(str(p) for p in proto.parameters_type)
            if proto_q not in (f"({params})", f"({params}){ret}"):
                continue
        matches.append(m)
    if not matches:
        raise ValueError(f"Method '{query}' not found in {clazz.fullname}.")
    with_code = [m for m in matches if m.code_offset]
    if len(matches) > 1 and len(with_code) != 1:
        overloads = []
        for m in matches:
            proto = m.prototype
            ret = str(clazz.dex.get_type(proto.return_type_idx))
            params = "".join(str(p) for p in proto.parameters_type)
            overloads.append(f"  {m.name}({params}){ret}")
        raise ValueError(
            f"Method '{name}' is overloaded in {clazz.fullname}; "
            f"pass the full signature:\n" + "\n".join(overloads))
    return with_code[0] if with_code else matches[0]


def build_cfg(dex, method):
    """Build the CFG of one DexMethod. Returns {'blocks': [...], 'edges': [...]}."""
    bytecode = method.bytecode
    if not bytecode:
        raise ValueError(f"Method {method.cls.fullname}->{method.name} has no code.")
    insns = decode_instructions(bytecode)
    blocks = build_blocks(insns)

    out_blocks = []
    out_edges = []
    for b in blocks:
        lines = []
        for insn in b.insns:
            lines.append(f"{insn.offset:04x}: {disassemble(insn, bytecode, dex)}")
        out_blocks.append({
            "id": b.id,
            "offset": b.offset,
            "smali": lines,
        })
        for tgt, kind, label in b.edges:
            out_edges.append({"from": b.id, "to": tgt, "type": kind, "label": label})
    return {
        "method": f"{method.cls.fullname}->{method.name}",
        "blocks": out_blocks,
        "edges": out_edges,
    }


def _dot_escape(text: str) -> str:
    return (text.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", "\\n"))


def cfg_to_dot(cfg: dict) -> str:
    lines = ["digraph CFG {", '  rankdir=TB;',
             '  node [shape=box, fontname="monospace", fontsize=10];']
    for b in cfg["blocks"]:
        label = f"b{b['id']}:\\n" + "\\n".join(_dot_escape(s) for s in b["smali"])
        lines.append(f'  "b{b["id"]}" [label="{label}"];')
    for e in cfg["edges"]:
        lbl = f' [label="{_dot_escape(e["label"])}"]' if e["label"] else ""
        lines.append(f'  "b{e["from"]}" -> "b{e["to"]}"{lbl};')
    lines.append("}")
    return "\n".join(lines) + "\n"


def cfg_to_json(cfg: dict) -> str:
    return json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"


def build_method_cfg(dex, dalvik_class: str, method_query: str) -> dict:
    """Locate class+method in a parsed DEX and build its CFG."""
    clazz = dex.get_class(dalvik_class)
    if clazz is None:
        raise ValueError(f"Class {dalvik_class} not found in {dex.name or 'DEX'}.")
    method = find_method(clazz, method_query)
    return build_cfg(dex, method)
