const fs = require('fs');
const path = require('path');

async function test() {
  console.log("=== Starting Next.js Orchestration Pipeline E2E Test ===");
  const imagePath = 'D:\\Tyndall Labs\\imageworld\\public\\worlds\\home-room\\source\\0-home.jpg';
  
  if (!fs.existsSync(imagePath)) {
    console.error("[FAIL] Test image not found at: " + imagePath);
    process.exit(1);
  }

  console.log("Loading test image...");
  const fileBuffer = fs.readFileSync(imagePath);
  const fileBlob = new Blob([fileBuffer], { type: 'image/jpeg' });

  const formData = new FormData();
  formData.append('file', fileBlob, '0-home.jpg');
  formData.append('name', 'Test E2E World');

  console.log("Sending POST request to http://localhost:3000/api/generate (this could take up to a minute)...");
  
  const startTime = Date.now();
  try {
    const res = await fetch('http://localhost:3000/api/generate', {
      method: 'POST',
      body: formData,
    });

    const duration = ((Date.now() - startTime) / 1000).toFixed(2);
    console.log(`Request completed in ${duration} seconds.`);

    if (!res.ok) {
      console.error(`[FAIL] Pipeline call failed: ${res.status} - ${await res.text()}`);
      return;
    }

    const data = await res.json();
    console.log("[OK] Pipeline success result:", data);
  } catch (err) {
    console.error("[FAIL] Pipeline call threw exception:", err);
  }
}

test();
