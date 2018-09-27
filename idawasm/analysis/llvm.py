import logging
import itertools
import collections

import wasm
import wasm.opcodes

import ida_ua
import ida_pro
import ida_name
import ida_funcs
import ida_frame
import ida_bytes
import ida_struct


logger = logging.getLogger(__name__)


class LLVMAnalyzer(object):
    def __init__(self, proc):
        self.proc = proc

    # min size of the example prologue
    MIN_PROLOGUE_SIZE = 21

    def is_store(self, op):
        return op.id in (wasm.opcodes.OP_I32_STORE,
                         wasm.opcodes.OP_I64_STORE,
                         wasm.opcodes.OP_F32_STORE,
                         wasm.opcodes.OP_F64_STORE,
                         wasm.opcodes.OP_I32_STORE8,
                         wasm.opcodes.OP_I32_STORE16,
                         wasm.opcodes.OP_I64_STORE8,
                         wasm.opcodes.OP_I64_STORE16,
                         wasm.opcodes.OP_I64_STORE32)

    def get_store_size(self, insn):
        return {wasm.opcodes.OP_I32_STORE:   'i32',
                wasm.opcodes.OP_I64_STORE:   'i64',
                wasm.opcodes.OP_F32_STORE:   'f32',
                wasm.opcodes.OP_F64_STORE:   'f64',
                wasm.opcodes.OP_I32_STORE8:  'i8',
                wasm.opcodes.OP_I32_STORE16: 'i16',
                wasm.opcodes.OP_I64_STORE8:  'i8',
                wasm.opcodes.OP_I64_STORE16: 'i16',
                wasm.opcodes.OP_I64_STORE32: 'i32'}[insn.op.id]

    def get_frame_store(self, frame_pointer, bc):
        '''
        find patterns like::

            code:01F3 20 06                   get_local           $frame_pointer
            code:01F5 20 00                   get_local           $param0
            code:01F7 36 02 14                i32.store           0x14, align:2

        args:
          frame_pointer (int): local variable index of the frame pointer.
          bc (List[wasm.Instruction]): sequence of at least three instructions.

        returns:
          Dict[Str, any]: frame store metadata, including:
            - frame_offset (int): offset into frame of store.
            - element_size (str): size of element being written.
            - access_type (str): the string "store".
            - offset (int): offset into the bitcode of the reference instruction.

        raises:
          ValueError: if the given bc does not contain a frame store.
        '''
        if bc[0].op.id != wasm.opcodes.OP_GET_LOCAL:
            raise ValueError('not a store')

        if bc[1].op.id != wasm.opcodes.OP_GET_LOCAL:
            raise ValueError('not a store')

        if not self.is_store(bc[2].op):
            raise ValueError('not a store')

        if bc[0].imm.local_index != frame_pointer:
            raise ValueError('not a store')

        return {
            'offset': bc[0].len + bc[1].len,
            'access_type': 'store',
            'frame_offset': bc[2].imm.offset,
            'element_size': self.get_store_size(bc[2])
        }

    def is_load(self, op):
        return op.id in (wasm.opcodes.OP_I32_LOAD,
                         wasm.opcodes.OP_I64_LOAD,
                         wasm.opcodes.OP_F32_LOAD,
                         wasm.opcodes.OP_F64_LOAD,
                         wasm.opcodes.OP_I32_LOAD8_U,
                         wasm.opcodes.OP_I32_LOAD8_S,
                         wasm.opcodes.OP_I32_LOAD16_U,
                         wasm.opcodes.OP_I32_LOAD16_S,
                         wasm.opcodes.OP_I64_LOAD8_U,
                         wasm.opcodes.OP_I64_LOAD8_S,
                         wasm.opcodes.OP_I64_LOAD16_U,
                         wasm.opcodes.OP_I64_LOAD16_S,
                         wasm.opcodes.OP_I64_LOAD32_U,
                         wasm.opcodes.OP_I64_LOAD32_S)

    def get_load_size(self, insn):
        return {wasm.opcodes.OP_I32_LOAD:     'i32',
                wasm.opcodes.OP_I64_LOAD:     'i64',
                wasm.opcodes.OP_F32_LOAD:     'f32',
                wasm.opcodes.OP_F64_LOAD:     'f64',
                wasm.opcodes.OP_I32_LOAD8_U:  'i8',
                wasm.opcodes.OP_I32_LOAD8_S:  'i8',
                wasm.opcodes.OP_I32_LOAD16_U: 'i16',
                wasm.opcodes.OP_I32_LOAD16_S: 'i16',
                wasm.opcodes.OP_I64_LOAD8_U:  'i8',
                wasm.opcodes.OP_I64_LOAD8_S:  'i8',
                wasm.opcodes.OP_I64_LOAD16_U: 'i16',
                wasm.opcodes.OP_I64_LOAD16_S: 'i16',
                wasm.opcodes.OP_I64_LOAD32_U: 'i32',
                wasm.opcodes.OP_I64_LOAD32_S: 'i32'}[insn.op.id]

    def get_frame_load(self, frame_pointer, bc):
        # find patterns like:
        #
        #     code:0245 20 06                   get_local           $local6
        #     code:0247 28 02 14                i32.load            0x14, align:2
        if bc[0].op.id != wasm.opcodes.OP_GET_LOCAL:
            raise ValueError('not a load')

        if not self.is_load(bc[1].op):
            raise ValueError('not a load')

        if bc[0].imm.local_index != frame_pointer:
            raise ValueError('not a load')

        return {
            'offset': bc[0].len,
            'access_type': 'load',
            'frame_offset': bc[1].imm.offset,
            'element_size': self.get_load_size(bc[1])
        }

    def find_function_frame_references(self, function, frame_pointer):
        '''
        returns:
          Dict[int, Set[Dict[str, any]]]: mapping from frame_offset to set of frame references.
        '''
        buf = ida_bytes.get_many_bytes(function['offset'], function['size'])
        bc = list(wasm.decode.decode_bytecode(buf))

        offset = function['offset']
        SLICE_SIZE = 3
        references = collections.defaultdict(lambda: list())
        for i in range(len(bc) - SLICE_SIZE - 1):
            insns = bc[i:i+SLICE_SIZE]

            try:
                load = self.get_frame_load(frame_pointer, insns)
            except ValueError:
                pass
            else:
                load['offset'] += offset
                logger.debug('found function frame load at 0x%X', load['offset'])
                references[load['frame_offset']].append(load)

            try:
                store = self.get_frame_store(frame_pointer, insns)
            except ValueError:
                pass
            else:
                store['offset'] += offset
                logger.debug('found function frame store at 0x%X', store['offset'])
                references[store['frame_offset']].append(store)

            offset += bc[i].len

        return references

    def analyze_function_frame(self, function):
        # given a function prologue like the following:
        #
        #     23 80 80 80 80 00       get_global          $global0
        #     21 04                   set_local           $local4
        #     41 20                   i32.const           0x20
        #     21 05                   set_local           $local5
        #     20 04                   get_local           $local4
        #     20 05                   get_local           $local5
        #     6B                      i32.sub
        #     21 06                   set_local           $local6
        #
        # recognize that the function frame is 0x20 bytes.
        prologue = ida_bytes.get_many_bytes(function['offset'], self.MIN_PROLOGUE_SIZE)
        prologue_bc = list(itertools.islice(wasm.decode.decode_bytecode(prologue), 8))
        prologue_mnems = list(map(lambda bc: bc.op.id, prologue_bc))

        # pattern match on the LLVM function prologue.
        # obviously brittle.
        if prologue_mnems != [wasm.opcodes.OP_GET_GLOBAL,  # global frame pointer
                              wasm.opcodes.OP_SET_LOCAL,
                              wasm.opcodes.OP_I32_CONST,   # function frame size
                              wasm.opcodes.OP_SET_LOCAL,
                              wasm.opcodes.OP_GET_LOCAL,
                              wasm.opcodes.OP_GET_LOCAL,
                              wasm.opcodes.OP_I32_SUB,
                              wasm.opcodes.OP_SET_LOCAL]:   # frame pointer
            return

        global_frame_pointer = prologue_bc[0].imm.global_index
        frame_size = prologue_bc[2].imm.value
        local_frame_pointer = prologue_bc[7].imm.local_index

        # add a frame structure to the function
        f = ida_funcs.get_func(function['offset'])
        ida_frame.add_frame(f, 0x0, 0x0, frame_size)
        ida_struct.set_struc_name(f.frame, ('frame%d' % function['index']).encode('utf-8'))

        # ensure global variable $frame_stack is named appropriately
        ida_name.set_name(self.proc.globals[global_frame_pointer]['offset'], '$frame_stack')

        # re-map local variable to $frame_pointer
        ida_frame.add_regvar(f,
                             function['offset'],
                             function['offset'] + function['size'],
                             '$local%d' % local_frame_pointer,
                             '$frame_pointer',
                             '')

        frame_references = self.find_function_frame_references(function, local_frame_pointer)
        for frame_offset, refs in frame_references.items():
            member_name = 'field_%x' % (frame_offset)

            # pick largest element size for the element type
            flags = 0
            size = 0
            for ref in refs:
                fl = {'i8':  ida_bytes.FF_BYTE   | ida_bytes.FF_DATA,
                      'i16': ida_bytes.FF_WORD   | ida_bytes.FF_DATA,
                      'i32': ida_bytes.FF_DWORD  | ida_bytes.FF_DATA,
                      'i64': ida_bytes.FF_QWORD  | ida_bytes.FF_DATA,
                      'f32': ida_bytes.FF_FLOAT  | ida_bytes.FF_DATA,
                      'f64': ida_bytes.FF_DOUBLE | ida_bytes.FF_DATA,}[ref['element_size']]

                s = {'i8':  1,
                     'i16': 2,
                     'i32': 4,
                     'i64': 8,
                     'f32': 4,
                     'f64': 8,}[ref['element_size']]

                # by luck, FF_BYTE < FF_WORD < FF_DWORD < FF_QWORD,
                # so we can order flag values.
                if fl > flags:
                    flags = fl
                    size = s

            logger.debug('adding frame member %s to function %d', member_name, function['index'])
            ida_struct.add_struc_member(ida_struct.get_struc(f.frame),
                                        member_name,
                                        frame_offset,
                                        flags & 0xFFFFFFFF,
                                        None,
                                        size)

        # mark struct references
        for refs in frame_references.values():
            for ref in refs:
                insn = ida_ua.insn_t()
                if not ida_ua.decode_insn(insn, ref['offset']):
                    continue

                path = ida_pro.tid_array(1)
                path[0] = f.frame
                ida_bytes.op_stroff(insn, 0, path.cast(), 1, 0)

    def analyze_function_frames(self, functions):
        for function in functions.values():
            if function['imported']:
                continue

            if function['size'] <= self.MIN_PROLOGUE_SIZE:
                continue

            self.analyze_function_frame(function)

    def analyze(self):
        self.analyze_function_frames(self.proc.functions)
