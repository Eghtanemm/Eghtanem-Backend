<?php

use App\Http\Controllers\Api\AuthController;
use App\Http\Controllers\Api\CommentController;
use App\Http\Controllers\Api\LessonController;
use App\Http\Controllers\Api\LikeController;
use App\Http\Controllers\Api\NotificationController;
use App\Http\Controllers\Api\PasswordResetController;
use App\Http\Controllers\Api\PodcastController;
use App\Http\Controllers\Api\SubjectController;
use App\Http\Controllers\Api\VideoController;
use App\Http\Controllers\Auth\SocialAuthController;
use App\Models\Video;
use Illuminate\Support\Facades\Route;
use Illuminate\Support\Facades\Storage;

Route::get('/get-videos', function () {
    $acceptedVideos = Video::where('is_active', true)
        ->with('user:id,name,email')
        ->orderByDesc('created_at')
        ->get()
        ->map(function (Video $video) {
            return [
                'video_title' => $video->title,
                'video_description' => $video->description,
                'video_path' => Storage::url($video->video_url),
                'thumbnail_url' => $video->thumbnail_url ? Storage::url($video->thumbnail_url) : null,
                'video_status' => 'accepted',
                'username' => optional($video->user)->name,
                'email' => optional($video->user)->email,
                'created_at' => optional($video->created_at)->toIso8601String(),
                'video_id' => $video->id,
            ];
        });

    return response()->json($acceptedVideos);
});

Route::post('/auth/social', [SocialAuthController::class, 'handle']);

// Auth routes - rate limited
Route::middleware('throttle:auth-sensitive')->group(function () {
    Route::post('/register', [AuthController::class, 'register']);
    Route::post('/login', [AuthController::class, 'login']);
    Route::post('/refresh-token', [AuthController::class, 'refreshToken']);
});

Route::middleware('throttle:otp-sensitive')->group(function () {
    Route::post('/verify-otp', [AuthController::class, 'verifyOtp']);
    Route::post('/forgot-password', [PasswordResetController::class, 'sendCode']);
    Route::post('/verify-reset-code', [PasswordResetController::class, 'verifyCode']);
    Route::post('/reset-password', [PasswordResetController::class, 'resetPassword']);
});

// Protected routes
Route::middleware(['auth:sanctum'])->group(function () {
    // Auth
    Route::get('/user', [AuthController::class, 'user']);
    Route::post('/logout', [AuthController::class, 'logout']);

    // Videos
    Route::get('/videos/mine', [VideoController::class, 'myVideos']);
    Route::get('/videos/mine/{video}', [VideoController::class, 'myVideoDetails']);
    Route::get('/videos', [VideoController::class, 'index']);
    Route::get('/videos/{video}', [VideoController::class, 'show']);
    Route::get('/videos/{video}/stream', [VideoController::class, 'streamVideo']);
    Route::post('/videos/{video}/view', [VideoController::class, 'markAsViewed']);
    Route::post('/videos/upload', [VideoController::class, 'store']);

    // Comments
    Route::get('/videos/{video}/comments', [CommentController::class, 'index']);
    Route::post('/videos/{video}/comments', [CommentController::class, 'store']);
    Route::put('/videos/{video}/comments/{comment}', [CommentController::class, 'update']);
    Route::delete('/videos/{video}/comments/{comment}', [CommentController::class, 'destroy']);

    // Likes
    Route::post('/videos/{video}/toggle-like', [LikeController::class, 'toggle']);
    Route::get('/videos/{video}/like-status', [LikeController::class, 'status']);

    // Notifications
    Route::get('/notifications', [NotificationController::class, 'index']);
    Route::get('/notifications/unread-count', [NotificationController::class, 'unreadCount']);
    Route::post('/notifications/{notification}/mark-as-read', [NotificationController::class, 'markAsRead']);
    Route::post('/notifications/mark-all-as-read', [NotificationController::class, 'markAllAsRead']);

    // Subject and Lesson routes
    Route::get('/subjects', [SubjectController::class, 'index']);
    Route::get('/subjects/{subject}', [SubjectController::class, 'show']);
    Route::get('/lessons/{lesson}', [LessonController::class, 'show']);
    Route::get('/lessons/{lesson}/stream', [LessonController::class, 'stream']);

    // Podcasts
    Route::get('podcasts', [PodcastController::class, 'index']);
    Route::get('podcasts/{podcast}', [PodcastController::class, 'show']);
    Route::get('podcasts/{podcast}/stream', [PodcastController::class, 'stream']);
    Route::get('podcasts/{podcast}/download', [PodcastController::class, 'download']);
    Route::post('podcasts/{podcast}/play', [PodcastController::class, 'incrementPlays']);
});
