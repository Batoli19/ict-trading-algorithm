import codecs

try:
    with open('run_output.txt', 'rb') as f:
        raw = f.read()
        text = raw.decode('utf-16le', errors='ignore')
    
    with open('run_output_ascii.txt', 'w', encoding='ascii', errors='replace') as f:
        f.write(text)
    print("Decoded successfully.")
except Exception as e:
    print(f"Error: {e}")
