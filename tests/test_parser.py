"""
Unit tests for DNS parser

Test strategy:
1. Parse real DNS responses captured from `dig`
2. Verify header, question, and record parsing
3. Test name compression pointer handling
4. Test different record types (A, AAAA, NS, CNAME, MX, TXT, SOA)
5. Edge cases: truncated responses, malformed pointers, etc.

To capture real DNS responses:
    dig google.com @8.8.8.8 +noedns > response.txt
    Then use tcpdump or similar to get raw bytes

Or see capture_dns_responses.py for a helper script.
"""

import pytest
import struct
from pathlib import Path
from src.dns_resolver.parser import (
    DNSParser, DNSHeader, DNSQuestion, DNSRecord,
    RecordType, RecordClass, ResponseCode, format_rdata,
    parse_ipv4, parse_ipv6
)


class TestDNSHeader:
    """Test DNS header parsing (12 bytes)"""

    def test_parse_simple_query_header(self):
        """Parse a simple query header"""
        # Create a minimal query header
        # ID=1, QR=0 (query), Opcode=0, AA=0, TC=0, RD=1, RA=0, RCODE=0
        # QDCOUNT=1, ANCOUNT=0, NSCOUNT=0, ARCOUNT=0
        header_bytes = struct.pack('!HHHHHH', 1, 0x0100, 1, 0, 0, 0)
        
        parser = DNSParser()
        parser.packet = header_bytes + b'\x00' * 100  # Dummy packet
        header = parser._parse_header()

        assert header.id == 1
        assert header.qr == False  # Query
        assert header.opcode == 0
        assert header.rd == True   # Recursion desired
        assert header.rcode == ResponseCode.NOERROR
        assert header.qdcount == 1
        assert header.ancount == 0

    def test_parse_response_header(self):
        """Parse a response header"""
        # QR=1 (response), RD=1, RA=1, RCODE=NOERROR
        header_bytes = struct.pack('!HHHHHH', 42, 0x8180, 1, 1, 0, 0)
        
        parser = DNSParser()
        parser.packet = header_bytes + b'\x00' * 100
        header = parser._parse_header()

        assert header.id == 42
        assert header.qr == True   # Response
        assert header.rd == True
        assert header.ra == True
        assert header.rcode == ResponseCode.NOERROR
        assert header.qdcount == 1
        assert header.ancount == 1


class TestDNSNameParsing:
    """Test domain name parsing with compression pointers"""
    def test_parse_name_rejects_truncated_label(self):
        parser = DNSParser()
        parser.packet = b"\x05ab"
        parser.offset = 0
        with pytest.raises((IndexError, UnicodeDecodeError, struct.error, ValueError)):
            parser._parse_name()

    def test_parse_name_rejects_truncated_compression_pointer(self):
        parser = DNSParser()
        parser.packet = b"\xc0"
        parser.offset = 0
        with pytest.raises((IndexError, struct.error, ValueError)):
            parser._parse_name()

    def test_parse_name_rejects_pointer_outside_packet(self):
        parser = DNSParser()
        parser.packet = b"\xc0\xff"
        parser.offset = 0
        with pytest.raises((IndexError, RecursionError, ValueError)):
            parser._parse_name()

    def test_parse_name_rejects_compression_pointer_loop(self):
        # Offset 0 points back to itself.
        parser = DNSParser()
        parser.packet = b"\xc0\x00"
        parser.offset = 0
        with pytest.raises((RecursionError, IndexError, ValueError)):
            parser._parse_name()


    def test_parse_simple_name(self):
        """Parse a simple uncompressed name"""
        # google.com = \x06google\x03com\x00
        name_bytes = b'\x06google\x03com\x00'
        
        parser = DNSParser()
        parser.packet = name_bytes + b'\x00' * 100
        parser.offset = 0
        name = parser._parse_name()

        assert name == 'google.com'

    def test_parse_root_domain(self):
        """Parse root domain (.)"""
        name_bytes = b'\x00'
        
        parser = DNSParser()
        parser.packet = name_bytes + b'\x00' * 100
        parser.offset = 0
        name = parser._parse_name()

        assert name == ''  # Or should be '.' - clarify in implementation

    def test_parse_name_with_pointer(self):
        """Parse a name with compression pointer"""
        # This tests the pointer following logic
        # Format: label + pointer_to_suffix
        # Example: www + pointer_to_google.com
        
        # First, set up a packet with google.com at offset 12
        google_com = b'\x06google\x03com\x00'
        
        # Now create a name www with pointer to google.com
        # www = \x03www, then pointer to offset 12
        # Pointer format: 11xxxxxx xxxxxxxx (top 2 bits = 1, then 14-bit offset)
        pointer = struct.pack('!H', 0xC012)  # C0 = 11000000, 12 = offset 18 (where google_com actually starts)
        
        www_pointer = b'\x03www' + pointer
        
        # Full packet: [header] [www.pointer_to_google] [google.com]
        packet = b'\x00' * 12 + www_pointer + google_com
        
        parser = DNSParser()
        parser.packet = packet
        parser.offset = 12
        name = parser._parse_name()

        assert name == 'www.google.com'

    def test_parse_multiple_labels(self):
        """Parse name with multiple labels"""
        # mail.google.com = \x04mail\x06google\x03com\x00
        name_bytes = b'\x04mail\x06google\x03com\x00'
        
        parser = DNSParser()
        parser.packet = name_bytes + b'\x00' * 100
        parser.offset = 0
        name = parser._parse_name()

        assert name == 'mail.google.com'


class TestDNSQuestion:
    """Test DNS question section parsing"""

    def test_parse_question(self):
        """Parse a question section"""
        # google.com A IN
        name_bytes = b'\x06google\x03com\x00'
        qtype_qclass = struct.pack('!HH', RecordType.A, RecordClass.IN)
        question_bytes = name_bytes + qtype_qclass
        
        parser = DNSParser()
        parser.packet = question_bytes + b'\x00' * 100
        parser.offset = 0
        question = parser._parse_question()

        assert question.qname == 'google.com'
        assert question.qtype == RecordType.A
        assert question.qclass == RecordClass.IN


class TestDNSRecordTypes:
    """Test parsing different record types"""

    def test_parse_a_record(self):
        """Parse A record (IPv4)"""
        # google.com IN A 142.251.32.46
        rdata = struct.pack('!BBBB', 142, 251, 32, 46)
        ip = parse_ipv4(rdata)
        assert ip == '142.251.32.46'

    def test_parse_aaaa_record(self):
        """Parse a 16-byte AAAA RDATA value into eight IPv6 groups."""
        # 2607:f8b0:4004:0817:0000:0000:0000:200e
        rdata = bytes.fromhex("2607f8b040040817000000000000200e")
        assert parse_ipv6(rdata) == "2607:f8b0:4004:817:0:0:0:200e"

    def test_format_rdata_aaaa_record(self):
        """format_rdata should delegate AAAA records to the IPv6 formatter."""
        rdata = bytes.fromhex("20010db8000000000000000000000001")
        assert format_rdata(RecordType.AAAA, rdata) == "2001:db8:0:0:0:0:0:1"

    def test_invalid_a_record_length(self):
        """A record must be exactly 4 bytes"""
        with pytest.raises(ValueError):
            parse_ipv4(b'\x01\x02\x03')  # Only 3 bytes

    def test_invalid_aaaa_record_length(self):
        """AAAA record must be exactly 16 bytes"""
        with pytest.raises(ValueError):
            parse_ipv6(b'\x01' * 15)  # Only 15 bytes

    def test_format_rdata_txt(self):
        """format_rdata should format A records as dotted IPv4"""
        rdata = struct.pack('!BBBB', 142, 251, 32, 46)
        assert format_rdata(RecordType.A, rdata) == '142.251.32.46'

    def test_format_rdata_txt_record(self):
        """format_rdata should decode TXT records"""
        text = b'hello'
        rdata = bytes([len(text)]) + text
        assert format_rdata(RecordType.TXT, rdata) == 'hello'

    def test_format_rdata_cname_with_pointer(self):
        """CNAME rdata containing a pointer should resolve correctly"""
        google_com = b'\x06google\x03com\x00'          # 12 bytes, placed at offset 12
        pointer = struct.pack('!H', 0xC00C)             # points to offset 12

        # Packet layout: [12-byte header][google.com][pointer]
        packet = b'\x00' * 12 + google_com + pointer
        # google_com occupies offset 12–23, pointer occupies offset 24–25

        rdata_offset = 24   # where the pointer bytes actually sit in the packet
        cname_rdata = pointer  # the extracted rdata IS the pointer bytes

        result = format_rdata(RecordType.CNAME, cname_rdata, packet=packet, rdata_offset=rdata_offset)
        assert result == 'google.com'

    def test_format_rdata_mx_with_pointer(self):
        #MX rdata containing preference and pointer should resolve correctly
        google_com = b'\x06google\x03com\x00'
        preference = struct.pack('!H', 10)
        pointer = struct.pack('!H', 0xC00C)  # points to offset 12

        # Packet layout: [12-byte header][google.com][preference][pointer]
        packet = b'\x00' * 12 + google_com + preference + pointer
        # google_com: offset 12-23, preference: offset 24-25, pointer: offset 26-27

        rdata_offset = 24   # rdata starts where preference begins
        mx_rdata = preference + pointer

        result = format_rdata(RecordType.MX, mx_rdata, packet=packet, rdata_offset=rdata_offset)
        assert result == '10 google.com'

class TestCompleteMessage:
    """Test parsing complete DNS messages"""

    def test_parse_empty_response(self):
        """Parse a response with just header, no records"""
        # Build a minimal response
        header = struct.pack('!HHHHHH', 1, 0x8180, 1, 0, 0, 0)
        question = b'\x06google\x03com\x00' + struct.pack('!HH', RecordType.A, RecordClass.IN)
        
        packet = header + question
        
        parser = DNSParser()
        header_obj, questions, answers, authority, additional = parser.parse_message(packet)

        assert header_obj.id == 1
        assert len(questions) == 1
        assert questions[0].qname == 'google.com'
        assert len(answers) == 0
        assert len(authority) == 0
        assert len(additional) == 0


class TestRealDNSResponses:
    """Test against captured real DNS responses"""

    def test_parse_google_a_response(self):
        """Parse real response for google.com A query"""
        filepath = Path(__file__).parent.parent / 'docs' / 'responses' / 'google.com-A.bin'
        if not filepath.exists():
            pytest.skip("Run scripts/capture_real_response.py first")
        
        with open(filepath, 'rb') as f:
            data = f.read()

        parser = DNSParser()
        header, questions, answers, authority, additional = parser.parse_message(data)

        assert header.qr == True
        assert header.rcode == ResponseCode.NOERROR
        assert questions[0].qname == 'google.com'
        assert len(answers) >= 1
        
        for ans in answers:
            print(f"{ans.name} {ans.ttl} IN {ans.type.name} "
                  f"{format_rdata(ans.type, ans.rdata, packet=data, rdata_offset=ans.rdata_offset)}")

    def test_parse_github_ns_response(self):
        """Parse real response for github.com NS query"""
        filepath = Path(__file__).parent.parent / 'docs' / 'responses' / 'github.com-NS.bin'
        if not filepath.exists():
            pytest.skip("Run scripts/capture_real_response.py first")
        
        with open(filepath, 'rb') as f:
            data = f.read()

        parser = DNSParser()
        header, questions, answers, authority, additional = parser.parse_message(data)

        assert header.qr == True
        assert questions[0].qname == 'github.com'
        assert len(answers) >= 1
        
        for ans in answers:
            print(f"{ans.name} {ans.ttl} IN {ans.type.name} "
                  f"{format_rdata(ans.type, ans.rdata, packet=data, rdata_offset=ans.rdata_offset)}")

    def test_parse_google_aaaa_response(self):
        """Parse real response for google.com AAAA query"""
        filepath = Path(__file__).parent.parent / 'docs' / 'responses' / 'google.com-AAAA.bin'
        if not filepath.exists():
            pytest.skip("Run scripts/capture_real_response.py first")
        
        with open(filepath, 'rb') as f:
            data = f.read()

        parser = DNSParser()
        header, questions, answers, authority, additional = parser.parse_message(data)

        assert header.qr == True
        assert header.rcode == ResponseCode.NOERROR
        assert questions[0].qname == 'google.com'
        assert len(answers) >= 1
        
        for ans in answers:
            formatted = format_rdata(ans.type, ans.rdata, packet=data, rdata_offset=ans.rdata_offset)
            print(f"{ans.name} {ans.ttl} IN {ans.type.name} {formatted}")
            if ans.type == RecordType.AAAA:
                assert ':' in formatted
                
class TestDNSEncoder:
    """Test DNS name encoding with compression pointer generation"""
    def test_encode_name_simple(self):
        """Encode a simple name with no prior context"""
        from src.dns_resolver.parser import DNSEncoder
        encoder = DNSEncoder()
        result = encoder.encode_name('google.com')
        assert result == b'\x06google\x03com\x00'

    def test_encode_name_with_compression(self):
        """Second name sharing a suffix should use a pointer"""
        from src.dns_resolver.parser import DNSEncoder
        encoder = DNSEncoder()
        
        first = encoder.encode_name('google.com')
        encoder.packet += first  # simulate writing it into the real packet
        
        second = encoder.encode_name('www.google.com')
        # "www" written as label, then pointer back to offset 0 (where google.com started)
        assert second == b'\x03www' + struct.pack('!H', 0xC000)

    def test_encode_header(self):
        """Encoding a header should be the exact reverse of parsing it"""
        from src.dns_resolver.parser import DNSEncoder, DNSHeader, ResponseCode
        
        header = DNSHeader(
            id=1, qr=False, opcode=0, aa=False, tc=False,
            rd=True, ra=False, rcode=ResponseCode.NOERROR,
            qdcount=1, ancount=0, nscount=0, arcount=0
        )
        
        encoder = DNSEncoder()
        result = encoder.encode_header(header)
        
        # Round-trip check: parse what we just encoded, should match original
        parser = DNSParser()
        parser.packet = result
        parsed_header = parser._parse_header()
        
        assert parsed_header == header

    def test_encode_question(self):
        """Encoding a question should round-trip through the parser correctly"""
        from src.dns_resolver.parser import DNSEncoder, DNSQuestion, RecordType, RecordClass
        
        question = DNSQuestion(qname='google.com', qtype=RecordType.A, qclass=RecordClass.IN)
        
        encoder = DNSEncoder()
        result = encoder.encode_question(question)
        
        parser = DNSParser()
        parser.packet = result
        parser.offset = 0
        parsed_question = parser._parse_question()
        
        assert parsed_question == question

    def test_encode_message_full_query(self):
        """Build a complete query, encode it, then parse it back — full round trip"""
        from src.dns_resolver.parser import DNSEncoder, DNSHeader, DNSQuestion, RecordType, RecordClass, ResponseCode
        
        header = DNSHeader(
            id=1234, qr=False, opcode=0, aa=False, tc=False,
            rd=True, ra=False, rcode=ResponseCode.NOERROR,
            qdcount=1, ancount=0, nscount=0, arcount=0
        )
        question = DNSQuestion(qname='google.com', qtype=RecordType.A, qclass=RecordClass.IN)
        
        encoder = DNSEncoder()
        message = encoder.encode_message(header, [question])
        
        # Now parse it back fully
        parser = DNSParser()
        parsed_header, parsed_questions, answers, authority, additional = parser.parse_message(message)
        
        assert parsed_header.id == 1234
        assert parsed_header.rd == True
        assert len(parsed_questions) == 1
        assert parsed_questions[0].qname == 'google.com'
        assert parsed_questions[0].qtype == RecordType.A

# Test fixtures for common data
@pytest.fixture
def simple_query():
    """A simple DNS query for google.com A"""
    header = struct.pack('!HHHHHH', 1, 0x0100, 1, 0, 0, 0)
    question = b'\x06google\x03com\x00' + struct.pack('!HH', RecordType.A, RecordClass.IN)
    return header + question


@pytest.fixture
def simple_response():
    """A simple DNS response with one A record"""
    # Response to google.com A query
    header = struct.pack('!HHHHHH', 1, 0x8180, 1, 1, 0, 0)
    question = b'\x06google\x03com\x00' + struct.pack('!HH', RecordType.A, RecordClass.IN)
    
    # Answer: google.com 300 IN A 142.251.32.46
    answer_name = b'\xc0\x0c'  # Pointer to question name
    answer_rdata = struct.pack('!BBBB', 142, 251, 32, 46)
    answer = answer_name + struct.pack('!HHI', RecordType.A, RecordClass.IN, 300) + \
             struct.pack('!H', 4) + answer_rdata
    
    return header + question + answer


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
