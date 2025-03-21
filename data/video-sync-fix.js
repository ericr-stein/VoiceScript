// Video synchronization fix script
document.addEventListener('DOMContentLoaded', function() {
    // Get the video element
    var video = document.getElementById('player');
    
    if (video) {
        // Create a robust initialization function
        function initializeVideoSync() {
            if (video.readyState >= 2) {  // HAVE_CURRENT_DATA or better
                // Initialize timestamp synchronization
                video.currentTime = 0.0;
                highlightFunction();
                
                // Ensure ontimeupdate handler is properly bound
                if (!video._highlightBound) {
                    video._highlightBound = true;
                    video.ontimeupdate = function() { highlightFunction(); };
                }
                console.log("Video sync fixed and initialized successfully");
            } else {
                // Try again in a short while if video not ready
                setTimeout(initializeVideoSync, 100);
            }
        }
        
        // Multiple event listeners for cross-browser compatibility
        video.addEventListener('loadeddata', initializeVideoSync);
        video.addEventListener('loadedmetadata', initializeVideoSync);
        
        // Also try initial sync
        if (video.readyState >= 2) {
            initializeVideoSync();
        } else {
            // Fallback timeout-based initialization
            setTimeout(initializeVideoSync, 500);
        }
    }
});
