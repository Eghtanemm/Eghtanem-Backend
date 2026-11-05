<?php

namespace App\Http\Controllers\Api;

use App\Http\Controllers\Controller;
use App\Models\Like;
use App\Models\Video;
use App\Models\VideoView;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Response;
use Illuminate\Support\Facades\Storage;
use Symfony\Component\HttpFoundation\StreamedResponse;
use Illuminate\Http\Request;

/**
 * @OA\Tag(
 *     name="Videos",
 *     description="API Endpoints for video management"
 * )
 */
class VideoController extends Controller
{
    /**
     * @OA\Get(
     *     path="/api/videos",
     *     summary="List all active videos",
     *     description="Returns a paginated list of active videos with pagination metadata",
     *     tags={"Videos"},
     *     @OA\Parameter(
     *         name="page",
     *         in="query",
     *         description="Page number for pagination (starts from 1)",
     *         required=false,
     *         @OA\Schema(type="integer", default=1, minimum=1)
     *     ),
     *     @OA\Parameter(
     *         name="per_page",
     *         in="query",
     *         description="Number of items per page (default: 20)",
     *         required=false,
     *         @OA\Schema(type="integer", default=20, minimum=1, maximum=100)
     *     ),
     *     @OA\Parameter(
     *         name="search",
     *         in="query",
     *         description="Search term to filter videos by title or description",
     *         required=false,
     *         @OA\Schema(type="string")
     *     ),
     *     @OA\Parameter(
     *         name="sort",
     *         in="query",
     *         description="Field to sort by (created_at, views_count, likes_count, comments_count)",
     *         required=false,
     *         @OA\Schema(type="string", enum={"created_at", "views_count", "likes_count", "comments_count"}, default="created_at")
     *     ),
     *     @OA\Parameter(
     *         name="order",
     *         in="query",
     *         description="Sort order (asc or desc)",
     *         required=false,
     *         @OA\Schema(type="string", enum={"asc", "desc"}, default="desc")
     *     ),
     *     @OA\Response(
     *         response=200,
     *         description="List of active videos with pagination metadata",
     *         @OA\JsonContent(
     *             type="object",
     *             @OA\Property(property="current_page", type="integer", example=1),
     *             @OA\Property(property="data", type="array", 
     *                 @OA\Items(
     *                     @OA\Property(property="id", type="integer", example=1),
     *                     @OA\Property(property="title", type="string", example="Sample Video"),
     *                     @OA\Property(property="description", type="string", example="Video description"),
     *                     @OA\Property(property="video_url", type="string", example="/storage/videos/sample-video.mp4"),
     *                     @OA\Property(property="thumbnail_url", type="string", example="/storage/thumbnails/sample-thumbnail.jpg"),
     *                     @OA\Property(property="views_count", type="integer", example=100),
     *                     @OA\Property(property="likes_count", type="integer", example=50),
     *                     @OA\Property(property="comments_count", type="integer", example=25),
     *                     @OA\Property(property="is_active", type="boolean", example=true),
     *                     @OA\Property(property="created_at", type="string", format="date-time"),
     *                     @OA\Property(property="updated_at", type="string", format="date-time"),
     *                     @OA\Property(
     *                         property="user",
     *                         type="object",
     *                         @OA\Property(property="id", type="integer", example=1),
     *                         @OA\Property(property="name", type="string", example="John Doe")
     *                     )
     *                 )
     *             ),
     *             @OA\Property(property="first_page_url", type="string", example="http://e8tanem.com/api/videos?page=1"),
     *             @OA\Property(property="from", type="integer", example=1),
     *             @OA\Property(property="last_page", type="integer", example=5),
     *             @OA\Property(property="last_page_url", type="string", example="http://e8tanem.com/api/videos?page=5"),
     *             @OA\Property(
     *                 property="links",
     *                 type="array",
     *                 @OA\Items(
     *                     @OA\Property(property="url", type="string", nullable=true),
     *                     @OA\Property(property="label", type="string", example="&laquo; Previous"),
     *                     @OA\Property(property="active", type="boolean")
     *                 )
     *             ),
     *             @OA\Property(property="next_page_url", type="string", example="http://e8tanem.com/api/videos?page=2", nullable=true),
     *             @OA\Property(property="path", type="string", example="http://e8tanem.com/api/videos"),
     *             @OA\Property(property="per_page", type="integer", example=20),
     *             @OA\Property(property="prev_page_url", type="string", example="http://e8tanem.com/api/videos?page=1", nullable=true),
     *             @OA\Property(property="to", type="integer", example=20),
     *             @OA\Property(property="total", type="integer", example=100)
     *         )
     *     ),
     *     @OA\Response(
     *         response=401,
     *         description="Unauthenticated",
     *         @OA\JsonContent(ref="#/components/schemas/UnauthorizedError")
     *     )
     * )
     */
    public function index(Request $request): JsonResponse
    {
        $query = Video::query()
            ->where('is_active', true)
            ->with(['user:id,name', 'lastComment.user:id,name'])
            ->withCount(['likes', 'comments']);

        if ($search = $request->get('search')) {
            $query->where(function ($q) use ($search) {
                $q->where('title', 'like', '%' . $search . '%')
                    ->orWhere('description', 'like', '%' . $search . '%');
            });
        }

        $userId = optional($request->user())->id;
        $prioritizeUnseen = $userId && !$search;

        if ($prioritizeUnseen) {
            $hasUnseen = (clone $query)->whereNotIn('id', function ($sub) use ($userId) {
                $sub->select('video_id')
                    ->from('video_views')
                    ->where('user_id', $userId);
            })->exists();

            if (!$hasUnseen && (clone $query)->exists()) {
                VideoView::query()->where('user_id', $userId)->delete();
            }

            $query->selectRaw(
                'EXISTS (SELECT 1 FROM video_views WHERE video_views.video_id = videos.id AND video_views.user_id = ?) as has_seen',
                [$userId]
            )->orderBy('has_seen');
        }

        $perPage = max(1, min((int) $request->integer('per_page', 20), 100));
        $mode = strtolower($request->get('mode', 'feed'));
        $sortField = $request->get('sort');
        $order = strtolower($request->get('order', 'desc')) === 'asc' ? 'asc' : 'desc';
        $sortable = ['created_at', 'views_count', 'likes_count', 'comments_count'];

        if ($mode === 'chronological') {
            $query->orderBy('created_at', $order);
        } elseif ($sortField && in_array($sortField, $sortable, true)) {
            $query->orderBy($sortField, $order);
        } else {
            $seed = $request->get('seed');

            if (!$seed) {
                $seed = $request->user()
                    ? 'user-' . $request->user()->id
                    : 'anon-' . $request->ip();
            }

            $query->feedRanked($seed);
        }

        $videos = $query->paginate($perPage);

        $collection = $videos->getCollection();
        $likedLookup = [];

        if ($userId = optional($request->user())->id) {
            $likedVideoIds = Like::query()
                ->where('user_id', $userId)
                ->whereIn('video_id', $collection->pluck('id'))
                ->pluck('video_id')
                ->all();

            $likedLookup = array_fill_keys($likedVideoIds, true);
        }

        $collection->transform(function (Video $video) use ($likedLookup) {
            $payload = $video->toArray();

            $payload['is_liked_by_viewer'] = $likedLookup[$video->id] ?? false;
            $payload['is_seen_by_viewer'] = isset($video->has_seen) ? (bool) $video->has_seen : null;
            unset($payload['has_seen']);
            $payload['engagement_overview'] = [
                'likes' => (int) ($video->likes_count ?? 0),
                'comments' => (int) ($video->comments_count ?? 0),
                'views' => (int) ($video->views_count ?? 0),
                'ranking_score' => isset($video->ranking_score)
                    ? round((float) $video->ranking_score, 4)
                    : null,
            ];

            $lastComment = $video->lastComment;
            $payload['last_comment'] = $lastComment ? [
                'id' => $lastComment->id,
                'content' => $lastComment->content,
                'created_at' => $lastComment->created_at,
                'user' => $lastComment->user ? [
                    'id' => $lastComment->user->id,
                    'name' => $lastComment->user->name,
                ] : null,
            ] : null;

            return $payload;
        });

        $videos->setCollection($collection);

        return response()->json($videos);
    }

    /**
     * @OA\Get(
     *     path="/api/videos/{id}",
     *     summary="Get video details",
     *     tags={"Videos"},
     *     @OA\Parameter(
     *         name="id",
     *         in="path",
     *         required=true,
     *         description="ID of the video",
     *         @OA\Schema(type="integer")
     *     ),
     *     @OA\Response(
     *         response=200,
     *         description="Video details",
     *         @OA\JsonContent(
     *             @OA\Property(property="id", type="integer", example=1),
     *             @OA\Property(property="title", type="string", example="Sample Video"),
     *             @OA\Property(property="description", type="string", example="Video description"),
     *             @OA\Property(property="url", type="string", example="https://example.com/video.mp4"),
     *             @OA\Property(property="views_count", type="integer", example=100),
     *             @OA\Property(property="likes_count", type="integer", example=50),
     *             @OA\Property(property="comments_count", type="integer", example=25),
     *             @OA\Property(property="is_active", type="boolean", example=true)
     *         )
     *     ),
     *     @OA\Response(
     *         response=404,
     *         description="Video not found"
     *     )
     * )
     */
    public function show(Video $video, Request $request): JsonResponse
    {
        if (!$video->is_active) {
            return response()->json(['message' => 'Video not found'], 404);
        }

        $video->load('user:id,name');
        $video->loadCount(['likes', 'comments']);
        $video->increment('views_count');
        $this->markVideoAsSeen($request->user()->id, $video->id);

        return response()->json($video);
    }

    private function markVideoAsSeen(int $userId, int $videoId): void
    {
        VideoView::query()->insertOrIgnore([
            'user_id' => $userId,
            'video_id' => $videoId,
            'created_at' => now(),
            'updated_at' => now(),
        ]);
    }

    /**
     * @OA\Post(
     *     path="/api/videos/{id}/view",
     *     summary="Mark a video as viewed by the current user",
     *     description="Records that the authenticated user has watched this video, so it is deprioritized (moved after unseen videos) in their feed. Idempotent — safe to call more than once for the same video. Once every active video has been marked as viewed, the user's watch history resets automatically on their next feed request.",
     *     tags={"Videos"},
     *     security={{"bearerAuth": {}}},
     *     @OA\Parameter(
     *         name="id",
     *         in="path",
     *         required=true,
     *         description="ID of the video",
     *         @OA\Schema(type="integer")
     *     ),
     *     @OA\Response(
     *         response=200,
     *         description="View recorded",
     *         @OA\JsonContent(
     *             @OA\Property(property="seen", type="boolean", example=true)
     *         )
     *     ),
     *     @OA\Response(
     *         response=401,
     *         description="Unauthenticated",
     *         @OA\JsonContent(ref="#/components/schemas/UnauthorizedError")
     *     ),
     *     @OA\Response(
     *         response=404,
     *         description="Video not found"
     *     )
     * )
     */
    public function markAsViewed(Video $video, Request $request): JsonResponse
    {
        if (!$video->is_active) {
            return response()->json(['message' => 'Video not found'], 404);
        }

        $this->markVideoAsSeen($request->user()->id, $video->id);

        return response()->json(['seen' => true]);
    }

    /**
     * @OA\Get(
     *     path="/api/videos/{id}/stream",
     *     summary="Stream video content",
     *     description="Stream the video content with support for range requests and caching for mobile optimization",
     *     tags={"Videos"},
     *     security={{"bearerAuth": {}}},
     *     @OA\Parameter(
     *         name="id",
     *         in="path",
     *         required=true,
     *         description="ID of the video to stream",
     *         @OA\Schema(type="integer")
     *     ),
     *     @OA\Parameter(
     *         name="Range",
     *         in="header",
     *         required=false,
     *         description="Range header for partial content requests (e.g., bytes=0-1000)",
     *         @OA\Schema(type="string")
     *     ),
     *     @OA\Response(
     *         response=200,
     *         description="Full video content stream",
     *         @OA\Header(
     *             header="Content-Type",
     *             description="video/mp4",
     *             @OA\Schema(type="string")
     *         ),
     *         @OA\Header(
     *             header="Accept-Ranges",
     *             description="bytes",
     *             @OA\Schema(type="string")
     *         ),
     *         @OA\Header(
     *             header="Content-Length",
     *             description="Size of the video in bytes",
     *             @OA\Schema(type="integer")
     *         ),
     *         @OA\Header(
     *             header="Cache-Control",
     *             description="public, max-age=86400",
     *             @OA\Schema(type="string")
     *         )
     *     ),
     *     @OA\Response(
     *         response=206,
     *         description="Partial video content (for range requests)",
     *         @OA\Header(
     *             header="Content-Type",
     *             description="video/mp4",
     *             @OA\Schema(type="string")
     *         ),
     *         @OA\Header(
     *             header="Content-Range",
     *             description="bytes start-end/size",
     *             @OA\Schema(type="string")
     *         ),
     *         @OA\Header(
     *             header="Content-Length",
     *             description="Size of the requested range in bytes",
     *             @OA\Schema(type="integer")
     *         )
     *     ),
     *     @OA\Response(
     *         response=404,
     *         description="Video not found or file not available",
     *         @OA\JsonContent(ref="#/components/schemas/NotFoundError")
     *     ),
     *     @OA\Response(
     *         response=401,
     *         description="Unauthenticated",
     *         @OA\JsonContent(ref="#/components/schemas/UnauthorizedError")
     *     )
     * )
     */
    public function streamVideo(Video $video): StreamedResponse
    {
        if (!$video->is_active) {
            abort(404);
        }

        $storagePath = ltrim($video->video_url, '/');
        $path = storage_path('app/public/' . $storagePath);

        if (!file_exists($path)) {
            abort(404);
        }

        $this->markVideoAsSeen(request()->user()->id, $video->id);

        $size = filesize($path);
        $start = 0;
        $end = $size - 1;

        // Cache control headers for mobile optimization
        $headers = [
            'Content-Type' => 'video/mp4',
            'Accept-Ranges' => 'bytes',
            'Content-Length' => $size,
            'Cache-Control' => 'public, max-age=86400', // 24-hour cache
            'Content-Disposition' => 'inline',
        ];

        if (request()->header('Range')) {
            $range = str_replace('bytes=', '', request()->header('Range'));
            [$start, $end] = explode('-', $range);
            $end = $end ?: $size - 1;
            $length = $end - $start + 1;

            $headers['Content-Length'] = $length;
            $headers['Content-Range'] = "bytes $start-$end/$size";

            return response()->stream(
                function () use ($path, $start, $length) {
                    $handle = fopen($path, 'rb');
                    fseek($handle, $start);
                    echo fread($handle, $length);
                    fclose($handle);
                },
                206,
                $headers
            );
        }

        return response()->stream(
            function () use ($path) {
                $handle = fopen($path, 'rb');
                while (!feof($handle)) {
                    echo fread($handle, 8192); // Stream in 8KB chunks
                    flush();
                }
                fclose($handle);
            },
            200,
            $headers
        );
    }
    
    public function store(Request $request): JsonResponse
    {
        $user = $request->user();

        $validated = $request->validate([
            'title' => 'required|string|max:255',
            'description' => 'required|string',
            'video' => 'required|file|mimes:mp4,avi,mov,mkv,webm|max:153600', // 150MB
            'thumbnail' => 'nullable|image|max:5120',
        ]);

        $videoFile = $request->file('video');
        $videoFilename = uniqid('user_' . $user->id . '_') . '.' . $videoFile->getClientOriginalExtension();
        $videoPath = $videoFile->storeAs('videos', $videoFilename, 'public');

        $thumbnailPath = null;
        if ($request->hasFile('thumbnail')) {
            $thumbnailFile = $request->file('thumbnail');
            $thumbnailFilename = uniqid('thumb_' . $user->id . '_') . '.' . $thumbnailFile->getClientOriginalExtension();
            $thumbnailPath = $thumbnailFile->storeAs('thumbnails', $thumbnailFilename, 'public');
        }

        $video = Video::create([
            'title' => $validated['title'],
            'description' => $validated['description'],
            'video_url' => $videoPath,
            'thumbnail_url' => $thumbnailPath,
            'is_active' => false,
            'user_id' => $user->id,
        ]);

        return response()->json([
            'message' => 'تم رفع الفيديو وهو في انتظار المراجعة',
            'video' => $video->only([
                'id',
                'title',
                'description',
                'video_url',
                'thumbnail_url',
                'is_active',
                'created_at',
            ]),
        ], Response::HTTP_CREATED);
    }

    public function myVideos(Request $request): JsonResponse
    {
        $videos = Video::where('user_id', $request->user()->id)
            ->withCount(['likes', 'comments'])
            ->latest()
            ->get()
            ->map(function (Video $video) {
                return [
                    'id' => $video->id,
                    'title' => $video->title,
                    'description' => $video->description,
                    'video_url' => $this->publicStorageUrl($video->video_url),
                    'thumbnail_url' => $this->publicStorageUrl($video->thumbnail_url),
                    'views_count' => $video->views_count,
                    'likes_count' => $video->likes_count ?? 0,
                    'comments_count' => $video->comments_count ?? 0,
                    'status' => $video->is_active ? 'accepted' : 'pending',
                    'created_at' => $video->created_at,
                    'updated_at' => $video->updated_at,
                ];
            });

        return response()->json([
            'data' => $videos,
        ]);
    }

    public function myVideoDetails(Video $video, Request $request): JsonResponse
    {
        if ($video->user_id !== $request->user()->id) {
            return response()->json(['message' => 'Video not found'], 404);
        }

        $video->loadCount(['likes', 'comments']);

        return response()->json([
            'id' => $video->id,
            'title' => $video->title,
            'description' => $video->description,
            'video_url' => $this->publicStorageUrl($video->video_url),
            'thumbnail_url' => $this->publicStorageUrl($video->thumbnail_url),
            'views_count' => $video->views_count,
            'likes_count' => $video->likes_count,
            'comments_count' => $video->comments_count,
            'status' => $video->is_active ? 'accepted' : 'pending',
            'created_at' => $video->created_at,
            'updated_at' => $video->updated_at,
        ]);
    }

    /**
     * Turn a path stored on the public disk (e.g. "videos/x.mp4") into a
     * root-relative URL served by nginx ("/storage/videos/x.mp4"). Kept
     * relative so it doesn't depend on APP_URL being correct per environment.
     */
    private function publicStorageUrl(?string $path): ?string
    {
        if ($path === null || $path === '') {
            return null;
        }

        if (preg_match('#^https?://#i', $path) || str_starts_with($path, '/storage/')) {
            return $path;
        }

        return '/storage/' . preg_replace('#^/*(public/|storage/)?#', '', $path);
    }
}
