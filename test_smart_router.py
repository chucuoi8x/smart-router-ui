"""
Test suite cho smart-router (10.236.102.86:8320).
Chạy an toàn từ máy Windows, không ảnh hưởng production.
"""
import os
import re
import sys
import json
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

# --- Config ---
BASE_URL = "http://10.236.102.86:8320/v1"
API_KEY = os.environ.get("HERMES_CUSTOM_10_236_102_86_8320_API_KEY", "")
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

MODELS = [
    "claude-router-main",
    "claude-router-aibox-main",
    "claude-router-aibox-cheap",
    "claude-router-fast",
    "claude-router-engineering",
]

RESULTS_FILE = Path(__file__).parent / "test_results.json"

class Stats:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.total_time = 0.0

stats = Stats()

def log(section, title, detail=""):
    print(f"\n{'='*60}")
    print(f"[{section}] {title}")
    if detail:
        print(f"  {detail}")

def check(name, condition, detail="", error_msg=None):
    global stats
    status = "PASS" if condition else "FAIL"
    icon = "✓" if condition else "✗"
    print(f"  {icon} {name}: {status}")
    if detail:
        print(f"     Detail: {detail}")
    if not condition and error_msg:
        print(f"     Error: {error_msg}")
    if condition:
        stats.passed += 1
    else:
        stats.failed += 1

def test_basic_completion():
    """Test request/response cơ bản với OpenAI format."""
    log("TEST 1", "Basic Completion", "GPT-4o-mini single turn")
    
    payload = {
        "model": MODELS[1],
        "messages": [{"role": "user", "content": "Trả lời ngắn: Thế giới là gì?"}],
        "max_tokens": 50,
        "temperature": 0.7
    }
    
    try:
        t0 = time.time()
        resp = requests.post(f"{BASE_URL}/chat/completions", 
                            headers=HEADERS, json=payload, timeout=30)
        elapsed = time.time() - t0
        stats.total_time += elapsed
        
        print(f"  Status: {resp.status_code} | Time: {elapsed:.2f}s")
        
        # Check status code
        ok = resp.status_code == 200
        check("HTTP 200", ok, f"Code: {resp.status_code}", resp.text[:200])
        
        if ok:
            data = resp.json()
            
            # Response structure
            has_choices = "choices" in data
            check("Field 'choices'", has_choices, detail=data.get("choices", "N/A")[:100])
            
            if has_choices and len(data["choices"]) > 0:
                choice = data["choices"][0]
                
                has_message = "message" in choice
                check("Field 'message'", has_message, str(choice.get("message", "N/A"))[:150])
                
                if has_message:
                    msg = choice["message"]
                    has_role = msg.get("role") == "assistant"
                    has_content = bool(msg.get("content"))
                    check("Role 'assistant'", has_role, detail=msg.get("role"))
                    check("Has content", has_content, detail=str(msg.get("content", ""))[:100])
                    
                    has_finish = "finish_reason" in choice
                    check("Field 'finish_reason'", has_finish, detail=choice.get("finish_reason"))
            
            # Usage stats
            has_usage = "usage" in data
            check("Field 'usage'", has_usage, str(data.get("usage", "N/A")))
            
            if has_usage:
                usage = data["usage"]
                prompt_tokens = usage.get("prompt_tokens", 0)
                comp_tokens = usage.get("completion_tokens", 0)
                check("Tokens > 0", prompt_tokens > 0 and comp_tokens > 0, 
                      f"Prompt: {prompt_tokens}, Comp: {comp_tokens}")
        
    except Exception as e:
        check("Request success", False, detail=f"Exception: {e}")

def test_multiple_models():
    """Test nhiều model khác nhau."""
    log("TEST 2", "Multi-Model Support")
    
    for model in MODELS:
        print(f"\n--- Model: {model} ---")
        
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "1+1 bằng mấy? Trả lời số."}],
            "max_tokens": 10,
            "temperature": 0.3
        }
        
        try:
            t0 = time.time()
            resp = requests.post(f"{BASE_URL}/chat/completions",
                                headers=HEADERS, json=payload, timeout=30)
            elapsed = time.time() - t0
            
            ok = resp.status_code == 200
            if ok:
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                check(f"{model} response", True, 
                      detail=f"{elapsed:.2f}s — {content.strip()[:80]}")
            else:
                check(f"{model} response", False, 
                      detail=f"Status {resp.status_code} — {resp.text[:200]}")
        except Exception as e:
            check(f"{model} response", False, detail=f"Exception: {e}")

def test_error_handling():
    """Kiểm tra error handling."""
    log("TEST 3", "Error Handling")
    
    # 3a: Model không tồn tại
    payload = {
        "model": "this-model-does-not-exist-xyz",
        "messages": [{"role": "user", "content": "test"}],
        "max_tokens": 10
    }
    
    try:
        resp = requests.post(f"{BASE_URL}/chat/completions",
                            headers=HEADERS, json=payload, timeout=10)
        ok_4xx = 400 <= resp.status_code < 500
        check("404/400 cho model sai", ok_4xx, 
              detail=f"Status: {resp.status_code}, Body: {resp.text[:200]}")
    except Exception as e:
        check("404/400 cho model sai", False, detail=f"Exception: {e}")
    
    # 3b: Không có messages
    payload = {
        "model": MODELS[1],
        "messages": []
    }
    
    try:
        resp = requests.post(f"{BASE_URL}/chat/completions",
                            headers=HEADERS, json=payload, timeout=10)
        ok_4xx = 400 <= resp.status_code < 500
        check("400 cho messages rỗng", ok_4xx,
              detail=f"Status: {resp.status_code}, Body: {resp.text[:200]}")
    except Exception as e:
        check("400 cho messages rỗng", False, detail=f"Exception: {e}")
    
    # 3c: Wrong auth
    wrong_headers = {"Authorization": "Bearer invalid-key-12345", "Content-Type": "application/json"}
    try:
        resp = requests.post(f"{BASE_URL}/chat/completions",
                            headers=wrong_headers, json=payload, timeout=10)
        ok_401 = resp.status_code == 401
        check("401 khi sai key", ok_401,
              detail=f"Status: {resp.status_code}")
    except Exception as e:
        check("401 khi sai key", False, detail=f"Exception: {e}")

def test_concurrent():
    """Đa request đồng thời."""
    log("TEST 4", "Concurrent Requests", "10 requests đồng thời")
    
    responses = []
    
    def make_request(i):
        t0 = time.time()
        try:
            resp = requests.post(f"{BASE_URL}/chat/completions",
                                headers=HEADERS, json={
                                    "model": MODELS[1],
                                    "messages": [{"role": "user", "content": f"Test #{i}"}],
                                    "max_tokens": 20
                                }, timeout=30)
            elapsed = time.time() - t0
            return i, resp.status_code, elapsed
        except Exception as e:
            elapsed = time.time() - t0
            return i, f"EXC:{e}", elapsed
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(make_request, i) for i in range(10)]
        for future in as_completed(futures):
            idx, status, elapsed = future.result()
            responses.append((idx, status, elapsed))
            time.sleep(elapsed)
    
    # Sort by index
    responses.sort()
    
    total_ok = sum(1 for _, s, _ in responses if s == 200)
    avg_time = sum(e for _, _, e in responses) / len(responses)
    
    print(f"\nTotal OK: {total_ok}/10 | Avg: {avg_time:.2f}s")
    for idx, status, elapsed in responses:
        mark = "✓" if status == 200 else "✗"
        print(f"  {mark} Request #{idx}: {status} ({elapsed:.2f}s)")
    
    check("Concurrency", total_ok >= 8, 
          detail=f"{total_ok}/10 thành công, avg {avg_time:.2f}s")

def test_streaming():
    """Test streaming mode."""
    log("TEST 5", "Streaming Mode", "Stream chunks parsing")
    
    payload = {
        "model": MODELS[1],
        "messages": [{"role": "user", "content": "Đếm 1-5"}],
        "max_tokens": 20,
        "stream": True
    }
    
    try:
        t0 = time.time()
        resp = requests.post(f"{BASE_URL}/chat/completions",
                            headers=HEADERS, json=payload, stream=True, timeout=30)
        
        chunks = []
        got_chunk = False
        for line in resp.iter_lines():
            if line:
                line_str = line.decode('utf-8')
                if line_str.startswith('data: '):
                    data_str = line_str[6:]
                    if data_str == '[DONE]':
                        chunks.append("[DONE]")
                        break
                    try:
                        chunk = json.loads(data_str)
                        chunks.append(chunk)
                        got_chunk = True
                        
                        # Extract delta content
                        if "choices" in chunk and len(chunk["choices"]) > 0:
                            delta = chunk["choices"][0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                print(f"  Chunk: '{content}'", end="", flush=True)
                    except json.JSONDecodeError:
                        pass
        
        elapsed = time.time() - t0
        stats.total_time += elapsed
        
        ok = got_chunk and len(chunks) > 1
        check("Streaming response", ok, 
              detail=f"{len(chunks)} chunks, {elapsed:.2f}s, status {resp.status_code}")
        
    except Exception as e:
        check("Streaming response", False, detail=f"Exception: {e}")

def test_latency_baseline():
    """Đo latency baseline."""
    log("TEST 6", "Latency Baseline", "5 sequential requests")
    
    latencies = []
    
    for i in range(5):
        t0 = time.time()
        try:
            resp = requests.post(f"{BASE_URL}/chat/completions",
                                headers=HEADERS, json={
                                    "model": MODELS[1],
                                    "messages": [{"role": "user", "content": f"Quick #{i}"}],
                                    "max_tokens": 5
                                }, timeout=15)
            elapsed = time.time() - t0
            latencies.append(elapsed)
            if resp.status_code == 200:
                print(f"  Req #{i}: {elapsed*1000:.0f}ms ✓")
            else:
                print(f"  Req #{i}: {elapsed*1000:.0f}ms ✗ (status {resp.status_code})")
        except Exception as e:
            elapsed = time.time() - t0
            latencies.append(elapsed)
            print(f"  Req #{i}: {elapsed*1000:.0f}ms ✗ ({e})")
    
    if latencies:
        avg = sum(latencies) / len(latencies)
        min_lat = min(latencies)
        max_lat = max(latencies)
        check("Latency acceptable", avg < 10,
              detail=f"Avg: {avg:.2f}s, Min: {min_lat*1000:.0f}ms, Max: {max_lat*1000:.0f}ms")
    else:
        check("Latency baseline", False, detail="No successful responses")

def main():
    log("SMART ROUTER TEST SUITE", f"Base URL: {BASE_URL}", 
        f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    print(f"\nModels tested: {MODELS}")
    print(f"Requests will use bearer token authentication\n")
    
    # Run all tests
    test_basic_completion()
    test_multiple_models()
    test_error_handling()
    test_concurrent()
    test_streaming()
    test_latency_baseline()
    
    # Summary
    total = stats.passed + stats.failed
    log("SUMMARY", f"Passed: {stats.passed}/{total} | Failed: {stats.failed}/{total}",
        f"Total time: {stats.total_time:.2f}s")
    
    # Save results
    result_data = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_url": BASE_URL,
        "passed": stats.passed,
        "failed": stats.failed,
        "total": total,
        "details": {}
    }
    
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(result_data, f, indent=2, ensure_ascii=False)
    
    print(f"\nResults saved to: {RESULTS_FILE}")
    
    return 0 if stats.failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
