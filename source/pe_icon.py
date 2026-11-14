"""Build-time PE32+ icon resource writer (Microsoft PE resource format)."""
import struct


def with_icon(image, ico):
    data = bytearray(image)
    pe = struct.unpack_from('<I', data, 60)[0]
    optional = pe + 24
    assert data[pe:pe+4] == b'PE\0\0' and struct.unpack_from('<H', data, optional)[0] == 0x20b
    count = struct.unpack_from('<H', data, pe+6)[0]
    table = optional + struct.unpack_from('<H', data, pe+20)[0]
    file_align = struct.unpack_from('<I', data, optional+36)[0]
    section_align = struct.unpack_from('<I', data, optional+32)[0]
    align = lambda n,a: (n+a-1)//a*a
    last = struct.unpack_from('<8sIIIIIIHHI', data, table+40*(count-1))
    resource_rva = struct.unpack_from('<I',data,optional+128)[0]
    replacing = last[0].rstrip(b'\0') == b'.nwicon'
    if resource_rva and not replacing:
        raise ValueError('Refusing to replace unrecognized existing PE resources')
    if replacing:
        raw_start, rva = last[4], last[2]
        del data[raw_start:]
        slot = table+40*(count-1)
        prior_size = last[3]
    else:
        slot = table+40*count
        if slot+40 > struct.unpack_from('<I',data,optional+60)[0]:
            raise ValueError('No room for resource section header')
        raw_start = align(len(data),file_align)
        rva = align(last[2]+max(last[1],last[3]),section_align)
        count += 1
        prior_size = 0
    reserved,kind,nicons = struct.unpack_from('<HHH',ico)
    assert reserved == 0 and kind == 1 and nicons > 0
    icons = {}
    group = bytearray(struct.pack('<HHH',0,1,nicons))
    for i in range(nicons):
        entry=ico[6+16*i:22+16*i]
        width,height,colors,res,planes,bits,size,offset=struct.unpack('<BBBBHHII',entry)
        assert offset+size <= len(ico)
        icons[i+1]={0:(ico[offset:offset+size],0)}
        group += struct.pack('<BBBBHHIH',width,height,colors,res,planes,bits,size,i+1)
    tree={3:icons,14:{1:{0:(bytes(group),0)}}}
    resources=bytearray()
    leaves=[]
    def directory(node):
        offset=len(resources)
        resources.extend(struct.pack('<IIHHHH',0,0,0,0,0,len(node))+b'\0'*(8*len(node)))
        for i,(key,value) in enumerate(sorted(node.items())):
            if isinstance(value,dict):
                target=directory(value)|0x80000000
            else:
                target=len(resources)
                resources.extend(b'\0'*16)
                leaves.append((target,value))
            struct.pack_into('<II',resources,offset+16+i*8,key,target)
        return offset
    directory(tree)
    for slot_leaf,(blob,cp) in leaves:
        resources.extend(b'\0'*(-len(resources)%4))
        struct.pack_into('<IIII',resources,slot_leaf,rva+len(resources),len(blob),cp,0)
        resources.extend(blob)
    raw_size=align(len(resources),file_align)
    data.extend(b'\0'*(raw_start-len(data)))
    data.extend(resources+b'\0'*(raw_size-len(resources)))
    struct.pack_into('<8sIIIIIIHHI',data,slot,b'.nwicon\0',len(resources),rva,raw_size,raw_start,0,0,0,0,0x40000040)
    struct.pack_into('<H',data,pe+6,count)
    initialized=struct.unpack_from('<I',data,optional+8)[0]
    struct.pack_into('<I',data,optional+8,initialized+raw_size-prior_size)
    struct.pack_into('<I',data,optional+56,align(rva+len(resources),section_align))
    struct.pack_into('<I',data,optional+64,0)
    struct.pack_into('<II',data,optional+128,rva,len(resources))
    assert struct.unpack_from('<II',data,optional+144) == (0,0), 'Unexpected signed bootloader'
    return bytes(data)
