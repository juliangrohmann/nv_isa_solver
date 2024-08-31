import json
import math
import itertools
from enum import Enum
from typing import List
from collections import Counter, defaultdict
from concurrent import futures
from argparse import ArgumentParser
import traceback
import os

from disasm_utils import Disassembler, set_bit_range, get_bit_range
import table_utils
import parser
from parser import InstructionParser, Instruction
from life_range import analyse_live_ranges, get_interaction_ranges, InteractionType

operand_colors = [
    "#FE8386",
    "#F5B7DC",
    "#BF91F3",
    "#C9F3FF",
    "#FBDA73",
    "#72fc44",
    "#4e56fc",
    "#fc9b14",
    "#fc556e",
    "#256336",
]


class EncodingRangeType(str, Enum):
    CONSTANT = "constant"

    OPERAND = "operand"
    OPERAND_FLAG = "operand_flag"
    OPERAND_MODIFIER = "operand_modifier"

    FLAG = "flag"
    MODIFIER = "modifier"

    # Just the predicate.
    PREDICATE = "predicate"

    # Control code stuff.
    STALL_CYCLES = "stall"
    YIELD_FLAG = "y"
    READ_BARRIER = "r-bar"
    WRITE_BARRIER = "w-bar"
    BARRIER_MASK = "b-mask"
    REUSE_MASK = "reuse"


class EncodingRange:
    def __init__(
        self,
        type,
        start,
        length,
        operand_index=None,
        name=None,
        constant=None,
        group_id=None,
        inverse=False,
        shift=None,
        offset=None
    ):
        self.type = type
        self.start = start
        self.length = length
        self.operand_index = operand_index
        self.group_id = group_id
        self.name = name
        self.constant = constant
        self.inverse = inverse
        self.shift = shift
        self.offset = offset

    def to_json_obj(self):
        return self.__dict__

    def to_json(self):
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, json_str):
        json_dict = json.loads(json_str)
        return cls(**json_dict)

    @classmethod
    def from_json_obj(cls, obj):
        return cls(**obj)

    def __repr__(self):
        return self.to_json()


class EncodingRanges:
    def __init__(
        self,
        ranges,
        inst,
    ):
        self.ranges = ranges
        self.inst = inst

    def to_json_obj(self):
        ranges = [rng.to_json_obj() for rng in self.ranges]
        return {"ranges": ranges, "inst": self.inst.hex()}

    def to_json(self) -> str:
        return json.dumps(self.to_json_obj())

    @classmethod
    def from_json_obj(cls, obj):
        ranges = [EncodingRange.from_json_obj(rng) for rng in obj["ranges"]]
        return cls(ranges, bytes.fromhex(obj["inst"]))

    @classmethod
    def from_json(cls, json_str: str):
        return EncodingRanges.from_json_obj(json.loads(json_str))

    def _count(self, type) -> int:
        result = 0
        for range in self.ranges:
            result += 1 if range.type == type else 0
        return result

    def operand_count(self) -> int:
        result = 0
        for range in self.ranges:
            if range.type == EncodingRangeType.OPERAND:
                result = max(result, range.operand_index + 1)
        return result

    def modifier_count(self) -> int:
        return self._count(EncodingRangeType.MODIFIER)

    def _find(self, type) -> List[EncodingRange]:
        return list(filter(lambda x: x.type == type, self.ranges))

    def get_flags(self) -> List[str]:
        """
        Return a list of ranges in the encoding ranges.
        """
        return [rng.name for rng in self._find(EncodingRangeType.FLAG)]

    def encode(
        self,
        sub_operands,
        modifiers,
        flags=set(),
        operand_modifiers={},
        operand_flags={},
        predicate=7,
        stall_cycles=15,
        yield_flag=False,
        read_barrier=0,
        write_barrier=0,
        barrier_mask=0,
    ) -> bytearray:
        result = bytearray(b"\0" * 16)
        modifier_i = 0

        range_vals = {
            EncodingRangeType.PREDICATE: predicate,
            EncodingRangeType.STALL_CYCLES: stall_cycles,
            EncodingRangeType.YIELD_FLAG: 1 if yield_flag else 0,
            EncodingRangeType.READ_BARRIER: read_barrier,
            EncodingRangeType.WRITE_BARRIER: write_barrier,
            EncodingRangeType.BARRIER_MASK: barrier_mask,
        }

        written = defaultdict(int)
        for rng in self.ranges:
            value = None
            if rng.type == EncodingRangeType.CONSTANT:
                value = rng.constant
            elif rng.type == EncodingRangeType.OPERAND:
                value = sub_operands[rng.operand_index]
                if rng.offset: value -= rng.offset
                if rng.inverse: value ^= 2 ** rng.length - 1
                if rng.shift: value >>= rng.shift
            elif rng.type == EncodingRangeType.MODIFIER:
                if modifier_i < len(modifiers):
                    value = modifiers[modifier_i]
                    modifier_i += 1
            elif rng.type == EncodingRangeType.FLAG:
                if rng.name in flags:
                    value = 1
            elif (
                rng.type == EncodingRangeType.OPERAND_MODIFIER
                and rng.operand_index in operand_modifiers
            ):
                value = operand_modifiers[rng.operand_index]
            elif (
                rng.type == EncodingRangeType.OPERAND_FLAG
                and rng.operand_index in operand_flags
            ):
                value = 1 if rng.name in operand_flags[rng.operand_index] else 0
            elif rng.type in range_vals:
                value = range_vals[rng.type]

            if not value: continue
            value >>= written[rng.operand_index]
            # print(f"type {rng.type}: writing {value} to oper {rng.operand_index}: [{rng.start}, {rng.start + rng.length})")
            set_bit_range(result, rng.start, rng.start + rng.length, value)
            if rng.operand_index: written[rng.operand_index] = rng.length
        return result

    def is_invalid(self, mod):
        return "INVALID" in mod or "??" in mod

    def enumerate_modifiers(self, disassembler, initial_values=None):
        # NOTE: Enumerating with invalid modifiers in the instruction might be
        #      causing problems for us!

        modifiers = self._find(EncodingRangeType.MODIFIER)
        operand_values = [0] * self.operand_count()

        analysis_result = []
        if initial_values:
            _modi_values = list(initial_values)
        else:
            _modi_values = [get_bit_range(self.inst, rng.start, rng.start + rng.length) for rng in modifiers]

        for modifier_i, modifier in enumerate(modifiers):
            mod_result = self.enumerate_mod(disassembler, list(_modi_values), modifier, modifier_i)
            if mod_result is None:
                analysis_result.append([])
                continue

            for value, name in mod_result:
                if self.is_invalid(name):
                    print(f"Invalid mod result: {mod_result}, switching to dependent analysis")
                    analysis_result.append(self.enumerate_dependent_mod(disassembler, modifiers, modifier_i))
                    break
            else:
                analysis_result.append(mod_result)

        return analysis_result

    def enumerate_dependent_mod(self, disassembler, modifiers, idx):
        parsed = InstructionParser.parseInstruction(disassembler.disassemble(self.inst))
        print(f"{parsed.get_key()}: Enumerating dependent mods ({idx=})")
        val_ranges = [[v for v in range(2**m.length)] if i != idx else [0] for i,m in enumerate(modifiers)]
        bases = itertools.product(*val_ranges)
        results = []
        for basis in bases:
            mods = self.enumerate_mod(disassembler, list(basis), modifiers[idx], idx)
            if mods != None:
                results.append(mods)
        real_mods = []
        for probe_val in range(2**modifiers[idx].length):
            for res in results:
                valid = [name for val, name in res if val == probe_val and not self.is_invalid(name)]
                if len(valid):
                    real_mods.append((probe_val, valid[0]))
                    break
        print(f"Dependent result: {real_mods}")
        return real_mods

    def enumerate_mod(self, disassembler, initial_values, modifier, idx):
        insts = []
        values = initial_values
        operand_values = [0] * self.operand_count()
        for probe_val in range(2**modifier.length):
            values[idx] = probe_val
            insts.append(self.encode(operand_values, values))
        disasms = disassembler.disassemble_parallel(insts)
        ret = []

        try:
            first_modis = InstructionParser.parseInstruction(disasms[0]).modifiers
            second_modis = InstructionParser.parseInstruction(disasms[1]).modifiers
        except Exception:
            return None

        first_difference = find_modifier_difference(second_modis, first_modis)

        basis = Counter(first_modis)
        for modi in first_difference.split("."):
            basis[modi] -= 1
        counter_remove_zeros(basis)

        replace_original = False

        for i, asm in enumerate(disasms):
            try:
                asm_modis = InstructionParser.parseInstruction(asm).modifiers
            except Exception:
                continue
            name = basis_find_modifier_difference(basis, asm_modis)
            ret.append((i, name))
        return ret

    def enumerate_operand_modifiers(self, disassembler):
        operand_modifiers = self._find(EncodingRangeType.OPERAND_MODIFIER)
        modifiers = self._find(EncodingRangeType.MODIFIER)
        result = {}
        modi_values = [
            get_bit_range(self.inst, rng.start, rng.start + rng.length)
            for rng in modifiers
        ]
        operand_values = [0] * self.operand_count()

        for modifier in operand_modifiers:
            insts = []
            for modi_i in range(2**modifier.length):
                operand_modis = {}
                operand_modis[modifier.operand_index] = modi_i
                insts.append(
                    self.encode(
                        operand_values, modi_values, operand_modifiers=operand_modis
                    )
                )
            disasms = disassembler.disassemble_parallel(insts)

            current = []
            result[modifier.operand_index] = current
            comp = disasms[1]
            for i, asm in enumerate(disasms):
                try:
                    comp_operands = InstructionParser.parseInstruction(
                        comp
                    ).get_flat_operands()
                    asm_operands = InstructionParser.parseInstruction(
                        asm
                    ).get_flat_operands()
                    name = find_modifier_difference(
                        comp_operands[modifier.operand_index].modifiers,
                        asm_operands[modifier.operand_index].modifiers,
                    )
                except Exception:
                    continue
                # name = ".".join(asm_modis)
                comp = asm
                current.append((i, name))
        return result

    def generate_html_table(self) -> str:
        builder = table_utils.TableBuilder()
        builder.tbody_start()

        def seperator():
            builder.tr_start("smoll")
            for i in range(64):
                builder.push(str(i % 8), 1)
            builder.tr_end()

        seperator()
        current_length = 0
        builder.tr_start()
        for erange in self.ranges:
            if current_length == 8 * 8:
                builder.tr_end()
                seperator()
                builder.tr_start()

            bg_color = None
            if erange.operand_index is not None:
                bg_color = operand_colors[erange.operand_index]
            text = erange.name

            if not text:
                text = erange.type
                if erange.type == "operand_modifier" or erange.type == "modifier":
                    text = "modi"
                    if erange.group_id:
                        text += f" {erange.group_id}"

            vertical = (
                erange.type == EncodingRangeType.FLAG
                or erange.type == EncodingRangeType.OPERAND_FLAG
            )

            if erange.type == EncodingRangeType.CONSTANT:
                text = bin(erange.constant)[2:].zfill(erange.length)[::-1]
                for c in text:
                    builder.push(c, 1, bg=bg_color, vertical=vertical)
                current_length += erange.length
                continue
            elif erange.type == EncodingRangeType.OPERAND:
                text += f" {erange.operand_index}"

            length = erange.length
            # Current range is split across two rows.
            if current_length < 8 * 8 and current_length + length > 8 * 8:
                diff = 8 * 8 - current_length
                builder.push(text, diff, bg=bg_color, vertical=vertical)

                # insert the row seperator.
                builder.tr_end()
                seperator()
                builder.tr_start()
                length -= diff
            # Push the remainder range.
            builder.push(text, length, bg=bg_color, vertical=vertical)
            current_length += erange.length
        builder.tr_end()
        builder.tbody_end()
        builder.end()
        return builder.result


def basis_find_modifier_difference(basis: Counter[str], mutated: List[str]):
    mutated = Counter(mutated)

    difference = Counter(mutated)
    difference.subtract(basis)
    result = ""
    for name, count in difference.items():
        if len(name) == 0 or count <= 0:
            continue
        result += ".".join([name] * count) + "."
    return result


def find_modifier_difference(original: List[str], mutated: List[str]):
    original = Counter(original)
    mutated = Counter(mutated)

    difference = Counter(mutated)
    difference.subtract(original)
    result = ""
    for name, count in difference.items():
        if len(name) == 0 or count <= 0:
            continue
        result += ".".join([name] * count) + "."

    return result


def analyse_modifiers(original: List[str], mutated: List[str]):
    """
    analyse a given list of modifiers and determine if the modifier bit can be a flag.
    Can have false positives for flag detection but will be corrected by second stage.
    """
    original = Counter(original)
    mutated = Counter(mutated)

    difference = Counter(mutated)
    difference.subtract(original)

    flag_candidate = None
    not_flag = False  # is it definetly not a flag.
    effected = False  # is a modifier field effected.
    for name, count in difference.items():
        if count == 0:
            # Modifier is uneffected
            continue
        effected = True
        if count <= 0:
            not_flag = True
            flag_candidate = None
            continue

        if count == 1 and not not_flag:
            if flag_candidate is None:
                flag_candidate = name
            else:
                flag_candidate = None
                not_flag = True

    return (effected, flag_candidate)


class InstructionMutationSet:
    def __init__(self, inst, disasm: str, mutations, disassembler):
        self.inst = inst
        self.disasm = disasm
        self.parsed = InstructionParser.parseInstruction(self.disasm)
        self.mutations = mutations
        self.disassembler = disassembler

        # TODO: Maybe combine this into one map.
        self.operand_type_bits = set()
        self.opcode_bits = set()
        self.operand_value_bits = set()
        self.operand_modifier_bits = set()
        self.operand_modifier_bit_flag = {}
        self.instruction_modifier_bit_flag = {}
        self.bit_to_operand = {}
        self.bit_to_shift = {}
        self.bit_to_offset = {}
        self.predicate_bits = set()

        self.modifier_bits = set()
        self.modifier_groups = {}
        self.inverse = False

        self._analyse()

    def reset_modifier_groups(self):
        self.modifier_groups = {}

    def canonicalize_modifier_groups(self):
        """
        Canonicalize modifier groups by assigning groupless bit sequences groups
        """

        # Step 1: Assign a number to each range.
        max_group_id = None
        fill_mode = False
        fill_id = None

        bits = sorted(list(self.modifier_bits))
        for i, bit in enumerate(bits):
            if bit in self.modifier_groups:
                continue
                # When there is a discontinuity, we should change the group_id
            if fill_mode and i != 0 and bits[i - 1] != bit - 1:
                fill_mode = False

            if not fill_mode:
                max_group_id = max([0] + list(self.modifier_groups.values()))
                fill_mode = True
                fill_id = max_group_id + 1
                max_group_id = fill_id
            self.modifier_groups[bit] = fill_id

        # Step 2: Fix numbers
        max_num = 0
        num_map = {}
        bits = sorted(list(self.modifier_bits))
        for bit in bits:
            gid = self.modifier_groups[bit]
            if gid not in num_map:
                num_map[gid] = max_num + 1
                max_num += 1
            self.modifier_groups[bit] = num_map[gid]

    def _analyse(self):
        parsed_operands = self.parsed.get_flat_operands()
        self.key = self.parsed.get_key()
        for i_bit, inst, asm in self.mutations:
            # The disassembler refused to decode this instruction.
            asm = asm.strip()
            if len(asm) == 0:
                self.opcode_bits.add(i_bit)
                continue

            try:
                mutated_parsed = InstructionParser.parseInstruction(asm)
            except Exception as e:
                print("Couldn't parse", asm, e)
                print(traceback.format_exc())
                continue
            if self.parsed.get_key() != mutated_parsed.get_key():
                # NOTE: Should we only say this is a opcode bit if the base instruction is different.
                self.opcode_bits.add(i_bit)
                continue

            # FIXME: This won't be able to handle '[R1].asd'
            mutated_operands = mutated_parsed.get_flat_operands()

            if self.parsed.predicate != mutated_parsed.predicate:
                self.predicate_bits.add(i_bit)

            operand_effected = False
            # analyse operand values and operand modifiers.
            for i, (a, b) in enumerate(zip(mutated_operands, parsed_operands)):
                if not a.compare(b):
                    self.operand_value_bits.add(i_bit)
                    self.bit_to_operand[i_bit] = i
                    operand_effected = True
                else:
                    effected, flag = analyse_modifiers(b.modifiers, a.modifiers)
                    if effected:
                        self.bit_to_operand[i_bit] = i
                        self.operand_modifier_bits.add(i_bit)
                        operand_effected = True
                    if flag:
                        self.operand_modifier_bit_flag[i_bit] = flag
            if operand_effected:
                continue

            # Don't look for modifiers in the opcode section.
            # we will consider it a different instruction anways.
            if i_bit > 12:
                # analyse instruction modifiers.
                effected, flag = analyse_modifiers(
                    self.parsed.modifiers, mutated_parsed.modifiers
                )
                if effected:
                    self.modifier_bits.add(i_bit)
                if flag:
                    self.instruction_modifier_bit_flag[i_bit] = flag

    def compute_encoding_ranges(self):
        """
        Construct encoding ranges from the mutation set.

        """
        result = []
        current_range = None
        self.canonicalize_modifier_groups()

        def _push():
            nonlocal current_range
            if current_range:
                result.append(current_range)
            current_range = None

        for i in range(0, 8 * 16):
            new_range = None
            if i in self.modifier_bits:
                if i in self.instruction_modifier_bit_flag:
                    _push()
                    current_range = EncodingRange(
                        EncodingRangeType.FLAG,
                        i,
                        1,
                        name=self.instruction_modifier_bit_flag[i],
                    )
                    _push()
                    continue
                else:
                    new_range = EncodingRange(
                        EncodingRangeType.MODIFIER,
                        i,
                        1,
                        group_id=self.modifier_groups[i],
                    )
            elif i in self.predicate_bits:
                new_range = EncodingRange(EncodingRangeType.PREDICATE, i, 1)
            elif i in self.operand_value_bits:
                new_range = EncodingRange(
                    EncodingRangeType.OPERAND,
                    i,
                    1,
                    operand_index=self.bit_to_operand[i],
                )
            elif i in self.operand_modifier_bits:
                operand_index = self.bit_to_operand[i]
                # is this a flag
                if i in self.operand_modifier_bit_flag:
                    # Flush the current cell no matter what.
                    _push()
                    current_range = EncodingRange(
                        EncodingRangeType.OPERAND_FLAG,
                        i,
                        1,
                        operand_index=operand_index,
                        name=self.operand_modifier_bit_flag[i],
                    )
                    _push()
                    continue
                else:
                    new_range = EncodingRange(
                        EncodingRangeType.OPERAND_MODIFIER,
                        i,
                        1,
                        operand_index=operand_index,
                    )

            if new_range is None:
                control_code_ranges = [
                    (EncodingRangeType.STALL_CYCLES, 4),
                    (EncodingRangeType.YIELD_FLAG, 1),
                    (EncodingRangeType.READ_BARRIER, 3),
                    (EncodingRangeType.WRITE_BARRIER, 3),
                    (EncodingRangeType.BARRIER_MASK, 6),
                    (EncodingRangeType.REUSE_MASK, 4),
                ]

                offset = 13 * 8 + 1
                for rtype, length in control_code_ranges:
                    if (
                        i >= offset
                        and i < offset + length
                        and get_bit_range(self.inst, offset, offset + length) == 0
                    ):
                        new_range = EncodingRange(rtype, i, 1)
                        break
                    offset += length

            if new_range is None:
                new_range = EncodingRange(EncodingRangeType.CONSTANT, i, 1, constant=0)
            # Decide if we should extend the current range or not.
            if (
                current_range
                and new_range.type == current_range.type
                and new_range.operand_index == current_range.operand_index
                and (new_range.type != EncodingRangeType.CONSTANT or i != 8 * 8)
                and (
                    new_range.group_id is None
                    or new_range.group_id == current_range.group_id
                )
            ):
                current_range.length += 1
            else:
                # Push current range
                _push()
                current_range = new_range

            if current_range.shift is None and i in self.bit_to_shift:
                current_range.shift = self.bit_to_shift[i]
            if current_range.offset is None and i in self.bit_to_offset:
                current_range.offset = self.bit_to_offset[i]

            if current_range.type == EncodingRangeType.CONSTANT:
                current_range.constant |= ((self.inst[i // 8] >> (i % 8)) & 1) << (
                    current_range.length - 1
                )

        _push()

        return EncodingRanges(result, self.inst)


def set_bit(array: bytearray, i):
    bit_offset = i % 8
    array[i // 8] ^= 1 << bit_offset


def analysis_disambiguate_flags(
    disassembler: Disassembler, mset: InstructionMutationSet
) -> bool:
    """
    Analysis pass to disambiguate flags from modifiers by fliping adjacent bits.
    TODO: Operand modifier/flag disambiguation
    """
    modifier_mutations = []

    for bit in mset.instruction_modifier_bit_flag:
        inst_ = bytearray(mset.inst)
        set_bit(inst_, bit)
        set_bit(inst_, bit + 1)
        modifier_mutations.append((inst_, bit, bit + 1))

        if bit - 1 not in mset.instruction_modifier_bit_flag:
            inst_ = bytearray(mset.inst)
            set_bit(inst_, bit)
            set_bit(inst_, bit - 1)
            modifier_mutations.append((inst_, bit, bit - 1))
    if len(modifier_mutations) == 0:
        return False
    instructions, offsets, adj_offsets = zip(*modifier_mutations)

    disassembled = mset.disassembler.disassemble_parallel(instructions)
    changed = False
    for disasm, bit, adj in zip(disassembled, offsets, adj_offsets):
        if bit not in mset.instruction_modifier_bit_flag:
            continue  # Already eleminated.
        flag_name = mset.instruction_modifier_bit_flag[bit]
        if not disasm:
            continue
        try:
            parsed = InstructionParser.parseInstruction(disasm)
        except Exception:
            continue
        if parsed.get_key() != mset.key:
            continue
        # If the flag name is not in the disassembled instruction, this is not really a flag.
        if flag_name not in parsed.modifiers:
            changed = True
            mset.modifier_bits.add(adj)
            del mset.instruction_modifier_bit_flag[bit]
            if adj in mset.instruction_modifier_bit_flag:
                del mset.instruction_modifier_bit_flag[adj]
            mset.reset_modifier_groups()

    return changed


def analysis_disambiguate_operand_flags(
    disassembler: Disassembler, mset: InstructionMutationSet
) -> bool:
    modifier_mutations = []
    for bit in mset.operand_modifier_bit_flag:
        inst_ = bytearray(mset.inst)
        set_bit(inst_, bit)
        set_bit(inst_, bit + 1)
        modifier_mutations.append((inst_, bit, bit + 1))
        if bit - 1 not in mset.operand_modifier_bit_flag:
            inst_ = bytearray(mset.inst)
            set_bit(inst_, bit)
            set_bit(inst_, bit - 1)
            modifier_mutations.append((inst_, bit, bit - 1))
    if len(modifier_mutations) == 0:
        return

    instructions, offsets, adj_offsets = zip(*modifier_mutations)
    disassembled = mset.disassembler.disassemble_parallel(instructions)
    changed = False
    for disasm, bit, adj in zip(disassembled, offsets, adj_offsets):
        if bit not in mset.operand_modifier_bit_flag:
            continue
        flag_name = mset.operand_modifier_bit_flag[bit]
        if not disasm:
            continue
        try:
            parsed = InstructionParser.parseInstruction(disasm)
        except Exception:
            continue
        if parsed.get_key() != mset.key:
            continue
        mutated_operands = parsed.get_flat_operands()
        if flag_name not in mutated_operands[mset.bit_to_operand[bit]].modifiers:
            changed = True
            del mset.operand_modifier_bit_flag[bit]
            if adj in mset.operand_modifier_bit_flag:
                del mset.operand_modifier_bit_flag[adj]
    return changed

def disasm_value(value, mset, rng, disassembler, offset=0):
    code = bytearray(mset.inst)
    set_bit_range(code, rng.start - offset, rng.start + rng.length, value)
    disasm = disassembler.disassemble(code)
    parsed = InstructionParser.parseInstruction(disasm)
    opers = parsed.get_flat_operands()
    if rng.operand_index >= len(opers): return None, None
    return opers[rng.operand_index].get_operand_value(), disasm

def analysis_operand_fix(disassembler: Disassembler, mset: InstructionMutationSet):
    def is_pred(oper):
        return isinstance(oper, parser.RegOperand) and oper.reg_type == 'P'

    ranges = mset.compute_encoding_ranges()
    opers = mset.parsed.get_flat_operands()
    seen = set()
    for rng in ranges._find(EncodingRangeType.OPERAND):
        oper = opers[rng.operand_index]
        if not isinstance(oper, parser.IntIMMOperand) and not is_pred(oper): continue
        if (rng.length <= 2 and not is_pred(oper)) or rng.operand_index in seen: continue
        seen.add(rng.operand_index)
        val_zero, disasm_zero = disasm_value(0, mset, rng, disassembler)
        val_one, disasm_one = disasm_value(1, mset, rng, disassembler)
        if val_zero is None or val_one is None: continue

        diff = abs(val_one - val_zero) if is_pred(oper) else val_one - val_zero
        if diff < 1: continue
        missing = math.log2(diff)
        if missing != int(missing): continue
        if missing < 1: continue
        # print(f"Key={mset.parsed.get_key()}")
        # print(f"Zero value={val_zero}")
        # print(f"One value={val_one}")
        # print(f"Zero disasm={disasm_zero}")
        # print(f"One disasm={disasm_one}")
        shift = 0
        missing = int(missing)
        offsets = []
        failure = False
        if not is_pred(oper):
            for i in range(0, rng.length):
                enc_val = 1 << i
                disasm_val, disasm = disasm_value(enc_val, mset, rng, disassembler, offset=missing)
                if disasm_val is None:
                    failure = True
                    break
                offsets.append(disasm_val - enc_val)
                if disasm_val == enc_val:
                    print(f"{mset.parsed.get_key()}: shift by {i}")
                    mset.bit_to_shift[rng.start] = shift = i
                    break
        if failure:
            print(f"{mset.parsed.get_key()}: unexpected failure")
            continue
        if len(offsets) >= 8 and offsets.count(offsets[-1]) >= len(offsets) // 2 and offsets[-1] != 0:
            mset.bit_to_offset[rng.start] = off = offsets[-1]
            if (shift := [v - off for v in offsets].index(0)) != 0:
                mset.bit_to_shift[rng.start] = shift
                print(f"{mset.parsed.get_key()}: shift by {shift} with offset")
            print(f"{mset.parsed.get_key()}: offset by {offsets[-1]}")
        elif len(offsets) and offsets[-1] != 0:
            print(f"{mset.parsed.get_key()}: failed to correct")
            continue
        for i in range(1, ext := missing - shift + 1):
            mset.operand_value_bits.add(rng.start - i)
            mset.bit_to_operand[rng.start - i] = rng.operand_index
        print(f"{mset.parsed.get_key()}: extended by {ext - 1}")


def analysis_predicate_fix(disassembler: Disassembler, mset: InstructionMutationSet, ranges: EncodingRanges):
    opers = mset.parsed.get_flat_operands()
    for rng in ranges._find(EncodingRangeType.OPERAND):
        oper = opers[rng.operand_index]
        if not isinstance(oper, parser.RegOperand) or not oper.reg_type == 'P': continue
        code = bytearray(mset.inst)
        set_bit_range(code, rng.start, rng.start + rng.length, 1)
        disasm = disassembler.disassemble(code)
        parsed = InstructionParser.parseInstruction(disasm)
        disasm_val = parsed.get_flat_operands()[rng.operand_index].get_operand_value()
        # print(f"{int.from_bytes(code, "little")=}")
        # print(f"{mset.disasm=}")
        # print(f"{disasm=}")
        # print(f"{disasm_val=}")
        if disasm_val == 6:
            print(f"{mset.parsed.get_key()}: found inverse")
            rng.inverse = True

# def analysis_operand_fix(
#     disassembler: Disassembler, mset: InstructionMutationSet
# ) -> bool:
#     """
#     With operands like [UR10 + 0x1], a constant IMM of 0 changes the operand
#     signature and won't be removed with distillation, causing a discontinuity
#     in the operand or makes it look shorter than it actually is.
#
#     Same happens with some predicates too
#     """
#
#     operands = mset.parsed.get_flat_operands()
#
#     def mutate_test(operand_index, idx, adj):
#         inst = bytearray(mset.inst)
#         set_bit(inst, idx)
#         set_bit(inst, adj)
#         asm = disassembler.disassemble(inst)
#         if len(asm) == 0:
#             return
#         try:
#             parsed = InstructionParser.parseInstruction(asm)
#         except Exception:
#             return
#         if parsed.get_key() != mset.key:
#             return
#         mutated_operands = parsed.get_flat_operands()
#         if operands[operand_index] == mutated_operands[operand_index]:
#             return
#         mset.operand_value_bits.add(idx)
#         mset.bit_to_operand[idx] = operand_index
#
#     ranges = mset.compute_encoding_ranges()
#     operand_ranges = ranges._find(EncodingRangeType.OPERAND)
#
#     for i, rng in enumerate(operand_ranges):
#         operand = operands[rng.operand_index]
#         if not isinstance(operand, parser.Operand) or not (
#             isinstance(operand, parser.IntIMMOperand)
#             and isinstance(operand.parent, parser.AddressOperand)
#         ) or operand.parent is None or len(operand.parent.sub_operands) <= 1:
#             continue
#         # Not 100% sure about this, what if the bit is between bytes?
#         if rng.start % 8 != 0:
#             mutate_test(rng.operand_index, rng.start - 1, rng.start)
#         elif rng.length % 8 != 0:
#             mutate_test(rng.operand_index, rng.start + rng.length, rng.start)


def analysis_extend_modifiers(
    disassembler: Disassembler, mset: InstructionMutationSet
) -> bool:
    """
    analysis pass to try to extend modifier fields.
    """
    ranges = mset.compute_encoding_ranges()
    modifier_ranges = ranges._find(EncodingRangeType.MODIFIER)
    changed = False

    def analyse_adj(modi_bit, adj):
        nonlocal changed
        # NOTE: I am not 100% sure about this. Maybe we can remove this once
        #       we have post enumeration modifier splitting?
        if adj in mset.instruction_modifier_bit_flag:
            return

        array = bytearray(mset.inst)
        set_bit(array, modi_bit)
        original_asm = disassembler.disassemble(array)
        if len(original_asm) == 0:
            return
        original_parsed = InstructionParser.parseInstruction(original_asm)

        set_bit(array, adj)
        modi_asm = disassembler.disassemble(array)
        if len(modi_asm) == 0:
            return
        try:
            modi_parsed = InstructionParser.parseInstruction(modi_asm)
        except Exception:
            # TODO: Ideally this wouldn't happen.
            return

        if modi_parsed.get_key() != original_parsed.get_key():
            return
        # Check if there is any difference in modifiers.
        if modi_parsed.modifiers != original_parsed.modifiers:
            # Adjacent bit is a part of the modifier

            changed = adj not in mset.modifier_bits
            mset.modifier_bits.add(adj)
            if adj in mset.instruction_modifier_bit_flag:
                del mset.instruction_modifier_bit_flag[adj]

    for rng in modifier_ranges:
        # 1 0 0 0 1 usually works better.
        analyse_adj(rng.start, rng.start - 1)
        # analyse_adj(rng.start + rng.length // 2, rng.start - 1)
        analyse_adj(rng.start, rng.start + rng.length)
        # analyse_adj(rng.start + rng.length // 2, rng.start + rng.length)
    if changed:
        mset.reset_modifier_groups()
    return changed


def analysis_modifier_coalescing(
    disassembler: Disassembler, mset: InstructionMutationSet
) -> bool:
    changed = False
    ranges = mset.compute_encoding_ranges()

    for i, rng in enumerate(ranges.ranges[1:]):
        if rng.length > 2:
            continue
        if (
            ranges.ranges[i].type != EncodingRangeType.MODIFIER
            or rng.type != EncodingRangeType.CONSTANT
        ):
            continue
        if (
            len(ranges.ranges) <= i + 2
            or ranges.ranges[i + 2].type != EncodingRangeType.MODIFIER
        ):
            continue
        for i in range(rng.start, rng.start + rng.length):
            changed = True
            mset.modifier_bits.add(i)
    if changed:
        mset.reset_modifier_groups()

    return changed


def analysis_modifier_splitting(
    disassembler: Disassembler, mset: InstructionMutationSet
):
    """
    Split the modifier if there is independence between modifiers.
    """
    ranges = mset.compute_encoding_ranges()
    modifier_ranges = ranges._find(EncodingRangeType.MODIFIER)

    def analyse_adj(modi_bit, adj):
        inst = []
        array = bytearray(mset.inst)
        inst.append(array)

        array = bytearray(array)
        set_bit(array, modi_bit)
        inst.append(array)

        array = bytearray(array)
        set_bit(array, adj)
        inst.append(array)

        inst = disassembler.disassemble_parallel(inst)
        if "" in inst:
            return False
        try:
            orig, modi, adj = [InstructionParser.parseInstruction(asm) for asm in inst]
        except Exception:
            return False

        if len(set([orig.get_key(), modi.get_key(), adj.get_key()])) != 1:
            return False

        orig_difference = find_modifier_difference(orig.modifiers, modi.modifiers)
        if (
            len(orig_difference) == 0
            or "." in orig_difference[:-1]
            or orig_difference.startswith("INVALID")
        ):
            return False
        orig_difference = orig_difference[:-1]
        if (
            orig_difference in adj.modifiers
            and adj.modifiers != modi.modifiers
            and adj.modifiers != orig.modifiers
        ):
            count_orig = Counter(modi.modifiers)[orig_difference]
            count_adj = Counter(adj.modifiers)[orig_difference]
            return count_orig == count_adj

    def split_range(rng, i):
        next_group_id = max([0] + list(mset.modifier_groups.values())) + 1
        for i in range(i, rng.length):
            mset.modifier_groups[rng.start + i] = next_group_id

    for rng in modifier_ranges:
        for i in range(1, rng.length):
            if (
                analyse_adj(rng.start, rng.start + i)
                or analyse_adj(rng.start + i - 1, rng.start + i)
                or analyse_adj(rng.start, rng.start + i)
            ):
                split_range(rng, i)
                return True

    return False


INSTRUCTION_DESC_HEADER = """
    <style>
        .instruction-desc {
            font-weight: bold;
            padding: 5px;
            margin-top: 15px;
            margin-bottom: 15px;
        }

        .flat-operand-section {
            padding: 2px;
            margin: 2px;
            border-radius: 5px;
        }
    </style>
"""


class InstructionDescGenerator:
    def generate(self, instruction: Instruction, full_name: str):
        self.result = '<div class="instruction-desc">'
        self.result += f'<span class="base-name">{full_name}</span>'

        # Assign numbers to sub operands.
        flat_op_i = 0
        for op in instruction.operands:
            for sop in op.flatten():
                sop.flat_operand_index = flat_op_i
                flat_op_i += 1

        self.result += '<span class="operands"> &nbsp; '
        for i, op in enumerate(instruction.operands):
            if i != 0:
                self.result += ","
            self.result += " "
            self.visit(op)
        self.result += "</span>"

        self.result += "</div>"
        return self.result

    def visit(self, op: parser.Operand):
        if isinstance(op, parser.DescOperand):
            self.visitDescOperand(op)
        elif isinstance(op, parser.ConstantMemOperand):
            self.visitConstantMemOperand(op)
        elif isinstance(op, parser.IntIMMOperand):
            self.visitIntIMMOperand(op)
        elif isinstance(op, parser.FloatIMMOperand):
            self.visitFloatIMMOperand(op)
        elif isinstance(op, parser.AddressOperand):
            self.visitAddressOperand(op)
        elif isinstance(op, parser.RegOperand):
            self.visitRegOperand(op)
        elif isinstance(op, parser.AttributeOperand):
            self.visitAttributeOperand(op)

    def begin_section(self, op: parser.Operand):
        self.result += f"<span class='flat-operand-section' style='background-color:{operand_colors[op.flat_operand_index]}'>"

    def end_section(self):
        self.result += "</span>"

    def visitAttributeOperand(self, op):
        self.result += "a"
        self.visit(op.sub_operands[0])

    def visitDescOperand(self, op):
        self.result += "g" if op.g else ""
        self.result += "desc["
        self.visit(op.sub_operands[0])
        self.result += "]"
        if len(op.sub_operands) > 1:
            self.visit(op.sub_operands[1])

    def visitConstantMemOperand(self, op):
        self.result += "cx" if op.cx else "c"
        self.result += "["
        self.visit(op.sub_operands[0])
        self.result += "]"
        self.visit(op.sub_operands[1])

    def visitIntIMMOperand(self, op):
        self.begin_section(op)
        self.result += "INT_IMM"
        self.end_section()

    def visitFloatIMMOperand(self, op):
        self.begin_section(op)
        self.result += "FIMM"
        self.end_section()

    def visitAddressOperand(self, op):
        self.result += "["
        for i, sop in enumerate(op.sub_operands):
            if i != 0:
                self.result += "+"
            self.visit(sop)
        self.result += "]"

    def visitRegOperand(self, op):
        self.begin_section(op)
        self.result += op.get_operand_key()
        self.end_section()


def generate_modifier_table(title: str, modifiers, rng: EncodingRange):
    html_result = "<p>" + title
    builder = table_utils.TableBuilder()
    builder.tbody_start()
    for row in modifiers:
        builder.tr_start()
        builder.push(bin(row[0])[2:].zfill(rng.length))
        builder.push(row[1])
        builder.tr_end()
    builder.tbody_end()
    builder.end()

    html_result += builder.result + "</p>"
    return html_result


def counter_remove_zeros(counts: Counter):
    for name, count in list(counts.items()):
        if count == 0:
            del counts[name]


class InstructionSpec:
    """
    An instruction specification.
    """

    def __init__(
        self,
        disasm: str,
        parsed: Instruction,
        ranges: EncodingRanges,
        modifiers,
        operand_modifiers,
        operand_interactions=None,
    ):
        self.disasm = disasm
        self.parsed = parsed
        self.ranges = ranges
        self.modifiers = modifiers
        self.operand_modifiers = operand_modifiers
        self.operand_interactions = None

        self.empty_value = []
        self.all_modifiers = []
        for i, modifier_range in enumerate(modifiers):
            for value, name in modifier_range:
                self.all_modifiers.append((name[:-1], i, value))

        self.opcode_modis = self._get_opcode_modis()
        self.canonical_name = ".".join([self.parsed.base_name] + self.opcode_modis)

    def to_json_obj(self):
        return {
            "disasm": self.disasm,
            "parsed": self.parsed.to_json_obj(),
            "ranges": self.ranges.to_json_obj(),
            "modifiers": self.modifiers,
            "operand_modifiers": self.operand_modifiers,
            "operand_interactions": self.operand_interactions,
            "opcode_modis": self.opcode_modis,
            "canonical_name": self.canonical_name,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_json_obj())

    def _get_opcode_modis(self) -> str:
        inst_modifiers = set(self.parsed.modifiers)

        for modi, i, value in self.all_modifiers:
            if modi in inst_modifiers:
                inst_modifiers.remove(modi)

        return list(inst_modifiers)

    @classmethod
    def from_json_obj(cls, obj):
        return cls(
            obj["disasm"],
            Instruction.from_json_obj(obj["parsed"]),
            EncodingRanges.from_json_obj(obj["ranges"]),
            obj["modifiers"],
            obj["operand_modifiers"],
            operand_interactions=obj["operand_interactions"],
        )

    @classmethod
    def from_json(cls, json_str):
        return InstructionSpec.from_json_obj(json.loads(json_str))

    def get_modifier_values(self, modifiers):
        # Greedy algorithm for choosing the correct modifier values.
        counts = Counter(modifiers)

        for modi in self.opcode_modis:
            counts[modi] -= 1
            if counts[modi] < 0:
                return None

        def score_match(modifier_group):
            _counts = Counter(counts)
            match = True
            for modifier in modifier_group:
                if len(modifier) == 0:
                    continue
                if modifier not in counts:
                    match = False
                    break
            if not match:
                return 0
            for modifier in modifier_group:
                _counts[modifier] -= 1
                if _counts[modifier] < 0:
                    return 0
                counter_remove_zeros(_counts)
            score = sum(counts.values()) - sum(_counts.values())
            return score

        used_modis = set()
        modi_values = [0] * len(self.modifiers)
        change = True
        while len(counts) != 0 and change:
            change = False
            best_i = -1
            best_value = -1
            best_modi_group = None
            best_match = 0
            for modifier_group, i, value in self.all_modifiers:
                if i in used_modis:
                    continue
                modifier_group = [
                    operand
                    for operand in modifier_group.split(".")
                    if len(operand) != 0
                ]
                score = score_match(modifier_group)
                if score > best_match:
                    best_i = i
                    best_value = value
                    best_match = score
                    best_modi_group = modifier_group
            if best_match != 0:
                change = True
                modi_values[best_i] = best_value
                used_modis.add(best_i)
                for modifier in best_modi_group:
                    counts[modifier] -= 1
                    counter_remove_zeros(counts)

        flags = self.ranges.get_flags()
        used_flags = set()
        for name in counts:
            if name in flags:
                used_flags.add(name)
                counts[name] -= 1
        counter_remove_zeros(counts)

        if len(counts) != 0:
            print(
                "We failed to encode modifier values",
                modifiers,
                "current state",
                counts,
            )
            return None

        for operand_group, i, value in self.all_modifiers:
            if i in modi_values:
                continue
            if len(operand_group) != 0:
                continue
            modi_values[i] = value
        return modi_values, used_flags

    def get_minimal_modifiers(self) -> List[str]:
        modifiers = list(self.opcode_modis)
        for modi_group in self.modifiers:
            if len(modi_group) == 0:
                # this should never happen, but it does.
                continue

            if "" in [modi[1] for modi in modi_group]:
                continue
            modis = modi_group[0][1][:-1].split(".")
            modifiers += modis
        return modifiers

    def encode_for_life_range(self, modifiers=[]) -> bytearray:
        operands = self.parsed.get_flat_operands()
        operand_values = [0] * len(operands)
        reg_count = 0
        ureg_count = 0
        pred_count = 1
        upred_count = 1

        modifiers, flags = self.get_modifier_values(modifiers)
        if modifiers is None:
            return None, None
        registers = []
        predicates = []
        upredicates = []
        uregisters = []
        for i, operand in enumerate(operands):
            if isinstance(operand, parser.RegOperand):
                if operand.reg_type == "R":
                    operand_values[i] = reg_count * 16 + 16
                    registers.append((i, operand_values[i]))
                    reg_count += 1
                elif operand.reg_type == "P":
                    operand_values[i] = pred_count * 2
                    predicates.append((i, operand_values[i]))
                    pred_count += 1
                elif operand.reg_type == "UP":
                    operand_values[i] = upred_count * 2
                    upredicates.append((i, operand_values[i]))
                    upred_count += 1
                elif operand.reg_type == "UR":
                    operand_values[i] = ureg_count * 4 + 4
                    uregisters.append((i, operand_values[i]))
                    ureg_count += 1
        reg_files = {
            "GPR": registers,
            "PRED": predicates,
            "UPRED": upredicates,
            "UGPR": uregisters,
        }
        encoded = self.ranges.encode(
            operand_values, modifiers, yield_flag=False, read_barrier=0, write_barrier=0
        )
        return (reg_files, encoded)

    def encode(
        self,
        operand_values,
        operand_modifiers={},
        operand_flags={},
        modifiers=[],
        predicate=7,
        stall_cycles=15,
        yield_flag=False,
        read_barrier=0,
        write_barrier=0,
        barrier_mask=0,
    ) -> bytearray:
        modifiers, flags = self.get_modifier_values(modifiers)
        if modifiers is None:
            return None

        return self.ranges.encode(
            operand_values,
            modifiers=modifiers,
            flags=flags,
            operand_modifiers=operand_modifiers,
            operand_flags=operand_flags,
            predicate=predicate,
            stall_cycles=stall_cycles,
            yield_flag=yield_flag,
            read_barrier=read_barrier,
            write_barrier=write_barrier,
            barrier_mask=barrier_mask,
        )

    def analyse_operand_interactions(self, arch_code, nvdisasm):
        try:
            reg_files, encoded = self.encode_for_life_range(
                self.get_minimal_modifiers()
            )
            if encoded is None:
                return
            interaction_data, self.operand_interaction_raw = analyse_live_ranges(
                encoded, arch_code, nvdisasm=nvdisasm
            )
            interaction_ranges = get_interaction_ranges(interaction_data)
        except Exception as e:
            print("Couldn't analyse operands", self.disasm, e)
            print(traceback.format_exc())
            return
        if interaction_ranges is None:
            return
        result = {}
        for file_name, reg_ranges in interaction_ranges.items():
            range_to_operand = {begin: opx for opx, begin in reg_files[file_name]}
            result[file_name] = []
            for rng in reg_ranges:
                if rng[1] == "USED":
                    continue
                if rng[0] not in range_to_operand:
                    continue
                sub_operand_idx = range_to_operand[rng[0]]
                result[file_name].append((sub_operand_idx, rng[1], rng[2]))
        self.operand_interactions = result

    def generate_html(self):
        """
        Generate html for this instruction.
        """
        desc_generator = InstructionDescGenerator()
        html_result = desc_generator.generate(self.parsed, self.canonical_name)
        interaction_type_names = {
            InteractionType.READ: "READ",
            InteractionType.WRITE: "WRITE",
            InteractionType.READWRITE: "READ_WRITE",
        }
        if self.operand_interactions:
            operand_interactions = []
            operands = self.parsed.get_flat_operands()
            for file, file_usages in self.operand_interactions.items():
                for usg in file_usages:
                    op = operands[usg[0]]
                    operand_interactions.append((op, usg[1], usg[2]))
            operand_interactions = sorted(
                operand_interactions, key=lambda x: x[0].flat_operand_index
            )

            for i, operand_int in enumerate(operand_interactions):
                color = operand_colors[operand_int[0].flat_operand_index]
                html_result += f"""
                    <span class="flat-operand-section" style="background-color:{color}">
                    {interaction_type_names[operand_int[1]]} {operand_int[0].reg_type} ({operand_int[2]} slots)
                    </span>
                """
        html_result += f"<p> distilled: {self.disasm}</p>"
        html_result += f"<p> key: {self.parsed.get_key()}</p>"
        # html_result += repr(self.operand_interactions)
        html_result += self.ranges.generate_html_table()

        modifier_ranges = self.ranges._find(EncodingRangeType.MODIFIER)
        for i, rows in enumerate(self.modifiers):
            title = f"Modifier Group {i + 1}"
            html_result += generate_modifier_table(title, rows, modifier_ranges[i])

        operand_modifier_ranges = self.ranges._find(EncodingRangeType.OPERAND_MODIFIER)
        operand_modifier_ranges = {
            rng.operand_index: rng for rng in operand_modifier_ranges
        }

        for operand, modifiers in self.operand_modifiers.items():
            title = f"Operand {operand} operand modifiers"

            html_result += generate_modifier_table(
                title, modifiers, operand_modifier_ranges[operand]
            )
        return html_result


def analysis_run_fixedpoint(
    disassembler: Disassembler, mset: InstructionMutationSet, fn
):
    change = True
    while change:
        change = fn(disassembler, mset)


def instruction_analysis_pipeline(inst, disassembler, arch_code):
    inst = disassembler.distill_instruction(inst)
    asm = disassembler.disassemble(inst)

    mutations = disassembler.mutate_inst(inst, end=14 * 8 - 2)
    mutation_set = InstructionMutationSet(inst, asm, mutations, disassembler)
    parsed_inst = InstructionParser.parseInstruction(asm)
    analysis_run_fixedpoint(disassembler, mutation_set, analysis_disambiguate_flags)
    analysis_disambiguate_operand_flags(disassembler, mutation_set)
    analysis_operand_fix(disassembler, mutation_set)
    analysis_run_fixedpoint(disassembler, mutation_set, analysis_extend_modifiers)
    # analysis_modifier_coalescing(disassembler, mutation_set)
    analysis_run_fixedpoint(disassembler, mutation_set, analysis_modifier_splitting)
    ranges = mutation_set.compute_encoding_ranges()
    analysis_predicate_fix(disassembler, mutation_set, ranges)

    modifier_values = ranges.enumerate_modifiers(disassembler)
    operand_modifier_values = ranges.enumerate_operand_modifiers(disassembler)
    spec = InstructionSpec(
        asm, parsed_inst, ranges, modifier_values, operand_modifier_values
    )
    spec.analyse_operand_interactions(arch_code, disassembler.nvdisasm)
    return spec


class ISASpec:
    def __init__(self, instructions):
        self.instructions = instructions

    @classmethod
    def from_json_obj(cls, obj):
        return cls(
            {key: InstructionSpec.from_json_obj(value) for key, value in obj.items()}
        )

    @classmethod
    def from_json(cls, json_str):
        return ISASpec.from_json_obj(json.loads(json_str))

    @classmethod
    def from_file(cls, filename):
        with open(filename) as file:
            return ISASpec.from_json(file.read())

    def find_instruction(self, target_key, modifiers=[]):
        """
        Find best instruction spec best matching the modifiers.
        """
        modifiers = Counter(modifiers)

        score = -1
        best = None
        for key, inst in self.instructions.items():
            signature_key = inst.parsed.get_key()
            if signature_key != target_key:
                continue

            _modifiers = Counter(modifiers)
            match = True

            for modi in inst.opcode_modis:
                _modifiers[modi] -= 1
                if _modifiers[modi] < 0:
                    match = False
                    break
            if not match:
                continue

            new_score = sum(modifiers.values()) - sum(_modifiers.values())
            if new_score > score:
                best = inst
                score = new_score
        return best


def main():
    arg_parser = ArgumentParser()
    arg_parser.add_argument("--arch", default="SM90a")
    arg_parser.add_argument(
        "--arch_code", default=90, type=int
    )  # is this even correct for SM90a?
    arg_parser.add_argument("--cache_file", default="disasm_cache.txt")
    arg_parser.add_argument("--nvdisasm", default="nvdisasm")
    arg_parser.add_argument("--num_parallel", default=4, type=int)
    arg_parser.add_argument("--filter", default=None, type=str)

    arguments = arg_parser.parse_args()

    disassembler = Disassembler(arguments.arch, nvdisasm=arguments.nvdisasm)
    disassembler.load_cache(arguments.cache_file)

    analysis_result = {}
    while True:
        instructions = disassembler.find_uniques_from_cache()
        instructions = list(instructions.items())
        instructions = [
            (key, inst) for key, inst in instructions if key not in analysis_result
        ]
        if arguments.filter:
            instructions = [
                (key, inst) for key, inst in instructions if arguments.filter in key
            ]

        if len(instructions) == 0:
            print("No new instruction found, exiting")
            break

        print("Found", len(instructions), "instructions")

        with futures.ThreadPoolExecutor(max_workers=arguments.num_parallel) as executor:
            instruction_futures = {}
            for key, inst in instructions:
                future = executor.submit(
                    instruction_analysis_pipeline,
                    inst,
                    disassembler,
                    arguments.arch_code,
                )
                instruction_futures[key] = future

            for key, inst in instructions:
                spec = instruction_futures[key].result()
                analysis_result[key] = spec

    with open("isa.json", "w") as isa_json_file:
        analysis_serialized = {
            key: spec.to_json_obj() for key, spec in analysis_result.items()
        }
        isa_json_file.write(json.dumps(analysis_serialized))

    analysis_result = sorted(list(analysis_result.items()), key=lambda x: x[0])

    base_names = {}
    for key, spec in analysis_result:
        if spec.parsed.base_name not in base_names:
            base_names[spec.parsed.base_name] = []
        base_names[spec.parsed.base_name].append(spec)

    os.makedirs("output", exist_ok=True)

    for base in base_names:
        result = INSTRUCTION_DESC_HEADER + table_utils.INSTVIZ_HEADER
        for spec in base_names[base]:
            result += spec.generate_html()
        with open(f"output/{base}.html", "w") as file:
            file.write(result)

    with open("output/index.html", "w") as file:
        result = f"<h1> Nvidia {arguments.arch} Instruction Set Architecture</h1>"
        for base in base_names:
            result += f'<a href="{base}.html">{base}</a><br>'

        file.write(result)
    disassembler.dump_cache(arguments.cache_file)


if __name__ == "__main__":
    main()
