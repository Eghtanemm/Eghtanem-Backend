<?php

return [
    'temporary_file_upload' => [
        'disk' => 'local',
        // Global gate for every admin upload; each FileUpload field narrows this further.
        // Must include images (thumbnails) and every video/audio type the resources accept.
        'rules' => 'file|mimes:mp4,mov,webm,mp3,wav,jpg,jpeg,png,webp,gif|max:202400', // 200MB in kilobytes
        'directory' => 'livewire-tmp',
        'middleware' => null, // Can be 'none' or null
        'preview_mimes' => [   // File extensions (not MIME types) that get temporary preview URLs
            'mp4', 'mov', 'webm', 'mp3', 'wav',
            'jpg', 'jpeg', 'png', 'webp', 'gif',
        ],
        'max_upload_time' => 600, // Max duration (in seconds) before an upload gets invalidated
    ],
];