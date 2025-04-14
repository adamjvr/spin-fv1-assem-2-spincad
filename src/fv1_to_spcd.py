import sys
import os

def convert_fv1_to_spcd(input_asm_path, output_spcd_path):
    if not os.path.exists(input_asm_path):
        print(f"Error: {input_asm_path} not found.")
        return

    with open(input_asm_path, 'r') as f:
        asm_lines = f.readlines()

    # Prepare the XML structure of the .spcd file with ASM block
    spcd_header = '''<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<SpinCADPatch format="2">
  <PatchName>Imported FV-1 ASM</PatchName>
  <blockList>
    <block>
      <name>ASM</name>
      <x>100</x>
      <y>100</y>
      <params/>
      <ASM>
'''

    spcd_footer = '''      </ASM>
    </block>
  </blockList>
</SpinCADPatch>
'''

    # Indent each ASM line properly for XML
    asm_lines_indented = ["        " + line.rstrip() + "\n" for line in asm_lines]

    # Combine all parts
    full_spcd = spcd_header + ''.join(asm_lines_indented) + spcd_footer

    # Write the output
    with open(output_spcd_path, 'w') as f:
        f.write(full_spcd)

    print(f"Successfully converted {input_asm_path} to {output_spcd_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python fv1_to_spcd.py <input.asm> <output.spcd>")
    else:
        convert_fv1_to_spcd(sys.argv[1], sys.argv[2])
