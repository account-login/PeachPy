# This file is part of Peach-Py package and is licensed under the Simplified BSD license.
#    See license.rst for the full text of the license.

active_writer = None


class AssemblyWriter:
    def __init__(self, output_path, assembly_format, input_path=None):
        if assembly_format not in {"go", "nasm", "masm", "gas"}:
            raise ValueError("Unknown assembly format: %s" % assembly_format)
        self.assembly_format = assembly_format
        self.output_path = output_path
        self.output_header = ""
        self.comment_prefix = {
            "go": "//",
            "nasm": ";",
            "masm": ";",
            "gas": "#"
        }[assembly_format]

        import peachpy
        if input_path is not None:
            header_linea = ["%s Generated by PeachPy %s from %s"
                            % (self.comment_prefix, peachpy.__version__, input_path), "", ""]
        else:
            header_linea = ["%s Generated by PeachPy %s" % (self.comment_prefix, peachpy.__version__), "", ""]

        import os
        self.output_header = os.linesep.join(header_linea)

        self.previous_writer = None

    def __enter__(self):
        global active_writer
        self.previous_writer = active_writer
        active_writer = self
        self.output_file = open(self.output_path, "w")
        self.output_file.write(self.output_header)
        self.output_file.flush()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        global active_writer
        active_writer = self.previous_writer
        self.previous_writer = None
        if exc_type is None:
            self.output_file.close()
            self.output_file = None
        else:
            import os
            os.unlink(self.output_file.name)
            self.output_file = None
            raise

    def add_function(self, function):
        import peachpy.x86_64.function
        assert isinstance(function, peachpy.x86_64.function.ABIFunction), \
            "Function must be bindinded to an ABI before its assembly can be used"

        function_code = function.format(self.assembly_format)

        import os
        self.output_file.write(function_code + os.linesep)
        self.output_file.flush()


class ELFWriter:
    def __init__(self, output_path, abi, input_path=None):
        from peachpy.formats.elf.image import Image
        from peachpy.formats.elf.section import TextSection

        self.output_path = output_path
        self.previous_writer = None
        self.abi = abi
        self.image = Image(abi, input_path)
        self.text_section = TextSection(abi)
        self.image.bind_section(self.text_section, ".text")
        self.text_rela_section = None
        self.rodata_section = None

    def __enter__(self):
        global active_writer
        self.previous_writer = active_writer
        active_writer = self
        self.output_file = open(self.output_path, "w", buffering=0)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        global active_writer
        active_writer = self.previous_writer
        self.previous_writer = None
        if exc_type is None:
            self.image.symtab.bind()
            self.output_file.write(self.image.as_bytearray)
            self.output_file.close()
            self.output_file = None
        else:
            import os
            os.unlink(self.output_file.name)
            self.output_file = None
            raise

    def add_function(self, function):
        import peachpy.x86_64.function
        assert isinstance(function, peachpy.x86_64.function.ABIFunction), \
            "Function must be bindinded to an ABI before its assembly can be used"

        encoded_function = function.encode()

        function_offset = len(self.text_section.content)
        self.text_section.append(encoded_function.code_content)

        function_rodata_offset = 0
        if encoded_function.const_content:
            if self.rodata_section is None:
                from peachpy.formats.elf.section import ProgramBitsSection
                self.rodata_section = ProgramBitsSection(self.abi, allocate=True)
                self.image.bind_section(self.rodata_section, ".rodata")
            function_rodata_offset = len(self.rodata_section.content)
            self.rodata_section.append(encoded_function.const_content)

        # Map from symbol name to symbol index
        from peachpy.formats.elf.symbol import Symbol, SymbolBinding, SymbolType
        symbol_map = dict()
        for symbol in encoded_function.const_symbols:
            const_symbol = Symbol(self.abi)
            const_symbol.name_index = self.image.strtab.add(symbol.name)
            const_symbol.value = function_rodata_offset + symbol.offset
            const_symbol.content_size = symbol.size
            const_symbol.section_index = self.rodata_section.index
            const_symbol.binding = SymbolBinding.Local
            const_symbol.type = SymbolType.DataObject
            const_symbol_index = self.image.symtab.add(const_symbol)
            symbol_map[symbol.name] = const_symbol_index

        if encoded_function.code_relocations:
            if self.text_rela_section is None:
                from peachpy.formats.elf.section import Section, SectionType
                self.text_rela_section = Section(self.abi)
                self.text_rela_section.header.content_type = SectionType.AddendRelocations
                self.text_rela_section.header.content_size = 0
                self.text_rela_section.header.link_index = self.image.symtab.index
                self.text_rela_section.header.info = self.text_section.index
                self.text_rela_section.header.entry_size = 24 if self.abi.elf_bitness == 64 else 12
                self.image.bind_section(self.text_rela_section, ".rela.text")

            from peachpy.encoder import Encoder
            encoder = Encoder(self.abi.endianness, self.abi.elf_bitness)
            for relocation in encoded_function.code_relocations:
                offset = relocation.offset
                R_X86_64_PC32 = 2
                symbol_index = symbol_map[relocation.symbol]
                info = (symbol_index << 32) | R_X86_64_PC32
                addend = -4
                relocation = encoder.uint64(offset) + encoder.uint64(info) + encoder.uint64(addend & 0xFFFFFFFFFFFFFFFF)
                self.text_rela_section._content += relocation
                self.text_rela_section.header.content_size += len(relocation)

        function_symbol = Symbol(self.abi)
        function_symbol.name_index = self.image.strtab.add(function.name)
        function_symbol.value = function_offset
        function_symbol.content_size = len(encoded_function.code_content)
        function_symbol.section_index = self.text_section.index
        function_symbol.binding = SymbolBinding.Global
        function_symbol.type = SymbolType.Function
        self.image.symtab.add(function_symbol)


class MachOWriter:
    def __init__(self, output_path, abi):
        from peachpy.formats.macho.image import Image

        self.output_path = output_path
        self.previous_writer = None
        self.abi = abi
        self.image = Image(abi)

    def __enter__(self):
        global active_writer
        self.previous_writer = active_writer
        active_writer = self
        self.output_file = open(self.output_path, "w", buffering=0)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        global active_writer
        active_writer = self.previous_writer
        self.previous_writer = None
        if exc_type is None:
            self.output_file.write(self.image.as_bytearray)
            self.output_file.close()
            self.output_file = None
        else:
            import os
            os.unlink(self.output_file.name)
            self.output_file = None
            raise

    def add_function(self, function):
        import peachpy.x86_64.function
        assert isinstance(function, peachpy.x86_64.function.ABIFunction), \
            "Function must be bindinded to an ABI before its assembly can be used"

        encoded_function = function.encode()
        function_code = encoded_function.as_bytearray

        function_offset = len(self.image.text_section.content)

        self.image.text_section.append(function_code)

        from peachpy.formats.macho.symbol import Symbol, SymbolDescription, SymbolType, SymbolVisibility

        function_symbol = Symbol(self.abi)
        function_symbol.description = SymbolDescription.Defined
        function_symbol.type = SymbolType.SectionRelative
        function_symbol.visibility = SymbolVisibility.External
        function_symbol.string_index = self.image.string_table.add("_" + function.name)
        function_symbol.section_index = self.image.text_section.index
        function_symbol.value = function_offset
        self.image.symbols.append(function_symbol)


class MSCOFFWriter:
    def __init__(self, output_path, abi, input_path=None):
        from peachpy.formats.mscoff.image import Image
        from peachpy.formats.mscoff.section import TextSection

        self.output_path = output_path
        self.previous_writer = None
        self.abi = abi
        self.image = Image(abi, input_path)
        self.text_section = TextSection()
        self.image.add_section(self.text_section, ".text")

    def __enter__(self):
        global active_writer
        self.previous_writer = active_writer
        active_writer = self
        self.output_file = open(self.output_path, "w", buffering=0)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        global active_writer
        active_writer = self.previous_writer
        self.previous_writer = None
        if exc_type is None:
            self.output_file.write(self.image.as_bytearray)
            self.output_file.close()
            self.output_file = None
        else:
            import os
            os.unlink(self.output_file.name)
            self.output_file = None
            raise

    def add_function(self, function):
        import peachpy.x86_64.function
        assert isinstance(function, peachpy.x86_64.function.ABIFunction), \
            "Function must be bindinded to an ABI before its assembly can be used"

        encoded_function = function.encode()
        function_code = encoded_function.as_bytearray

        function_offset = len(self.text_section.content)
        self.text_section.write(function_code)

        from peachpy.formats.mscoff.symbol import SymbolEntry, SymbolType, StorageClass
        function_symbol = SymbolEntry()
        function_symbol.value = function_offset
        function_symbol.section_index = self.text_section.index
        function_symbol.symbol_type = SymbolType.function
        function_symbol.storage_class = StorageClass.external
        self.image.add_symbol(function_symbol, function.name)


class NullWriter:
    def __init__(self):
        pass

    def __enter__(self):
        global active_writer
        self.previous_writer = active_writer
        active_writer = None
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        global active_writer
        active_writer = self.previous_writer
        self.previous_writer = None
