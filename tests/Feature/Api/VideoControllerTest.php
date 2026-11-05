<?php

namespace Tests\Feature\Api;

use App\Models\Comment;
use App\Models\Like;
use App\Models\User;
use App\Models\Video;
use App\Models\VideoView;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Storage;
use Tests\TestCase;

class VideoControllerTest extends TestCase
{
    use RefreshDatabase;

    private User $user;

    protected function setUp(): void
    {
        parent::setUp();
        $this->user = User::factory()->create();
    }

    public function test_can_list_active_videos(): void
    {
        Video::factory()->count(3)->create([
            'is_active' => true,
            'user_id' => $this->user->id,
        ]);

        Video::factory()->count(2)->create([
            'is_active' => false,
            'user_id' => $this->user->id,
        ]);

        $response = $this->actingAs($this->user)->getJson('/api/videos');

        $response->assertOk()
            ->assertJsonCount(3, 'data');
    }

    public function test_can_view_active_video(): void
    {
        $video = Video::factory()->create([
            'is_active' => true,
            'user_id' => $this->user->id,
        ]);

        $response = $this->actingAs($this->user)->getJson("/api/videos/{$video->id}");

        $response->assertOk()
            ->assertJson([
                'id' => $video->id,
                'title' => $video->title,
            ]);
    }

    public function test_cannot_view_inactive_video(): void
    {
        $video = Video::factory()->create([
            'is_active' => false,
            'user_id' => $this->user->id,
        ]);

        $response = $this->actingAs($this->user)->getJson("/api/videos/{$video->id}");

        $response->assertNotFound();
    }

    public function test_viewing_video_increments_view_count(): void
    {
        $video = Video::factory()->create([
            'is_active' => true,
            'views_count' => 0,
            'user_id' => $this->user->id,
        ]);

        $response = $this->actingAs($this->user)->getJson("/api/videos/{$video->id}");
        $response->assertOk();

        $this->assertEquals(1, $video->fresh()->views_count);
    }

    public function test_feed_ranking_prioritizes_engagement_but_respects_chronology_mode(): void
    {
        $highEngagement = Video::factory()->create([
            'is_active' => true,
            'views_count' => 400,
            'user_id' => $this->user->id,
            'created_at' => now()->subHours(8),
        ]);

        $freshVideo = Video::factory()->create([
            'is_active' => true,
            'views_count' => 10,
            'user_id' => $this->user->id,
            'created_at' => now()->subMinutes(10),
        ]);

        Like::factory()->count(60)->create(['video_id' => $highEngagement->id]);
        Comment::factory()->count(25)->create(['video_id' => $highEngagement->id]);

        $feedResponse = $this->actingAs($this->user)->getJson('/api/videos?seed=test-seed');
        $feedResponse->assertOk()
            ->assertJsonPath('data.0.id', $highEngagement->id);

        $chronologicalResponse = $this->actingAs($this->user)
            ->getJson('/api/videos?mode=chronological');
        $chronologicalResponse->assertOk()
            ->assertJsonPath('data.0.id', $freshVideo->id);
    }

    public function test_feed_response_includes_last_comment_and_viewer_flags(): void
    {
        $video = Video::factory()->create([
            'is_active' => true,
            'views_count' => 25,
            'user_id' => $this->user->id,
        ]);

        Comment::factory()->create([
            'video_id' => $video->id,
            'created_at' => now()->subDay(),
        ]);

        $latestComment = Comment::factory()->create([
            'video_id' => $video->id,
            'content' => 'This is the freshest remark',
            'user_id' => User::factory()->create()->id,
            'created_at' => now(),
        ]);

        Like::factory()->create([
            'video_id' => $video->id,
            'user_id' => $this->user->id,
        ]);

        $response = $this->actingAs($this->user)->getJson('/api/videos?mode=chronological');

        $response->assertOk()
            ->assertJsonPath('data.0.last_comment.content', $latestComment->content)
            ->assertJsonPath('data.0.last_comment.user.id', $latestComment->user_id)
            ->assertJsonPath('data.0.is_liked_by_viewer', true)
            ->assertJsonPath('data.0.engagement_overview.likes', 1)
            ->assertJsonPath('data.0.engagement_overview.comments', 2)
            ->assertJsonPath('data.0.engagement_overview.views', 25);
    }

    public function test_watched_video_is_moved_to_the_end_but_still_returned(): void
    {
        $unseen = Video::factory()->create([
            'is_active' => true,
            'user_id' => $this->user->id,
            'created_at' => now()->subHour(),
        ]);

        $seen = Video::factory()->create([
            'is_active' => true,
            'user_id' => $this->user->id,
            'created_at' => now(),
        ]);

        $this->actingAs($this->user)->getJson("/api/videos/{$seen->id}")->assertOk();

        $response = $this->actingAs($this->user)->getJson('/api/videos?mode=chronological');

        $response->assertOk()->assertJsonCount(2, 'data');
        $this->assertEquals(
            [$unseen->id, $seen->id],
            collect($response->json('data'))->pluck('id')->all()
        );
        $this->assertFalse(collect($response->json('data'))->firstWhere('id', $unseen->id)['is_seen_by_viewer']);
        $this->assertTrue(collect($response->json('data'))->firstWhere('id', $seen->id)['is_seen_by_viewer']);
    }

    public function test_streaming_video_marks_it_seen_and_is_idempotent_across_range_requests(): void
    {
        $path = 'videos/test-seen-' . uniqid() . '.mp4';
        Storage::disk('public')->put($path, str_repeat('a', 2048));

        $video = Video::factory()->create([
            'is_active' => true,
            'user_id' => $this->user->id,
            'video_url' => $path,
        ]);

        try {
            $this->actingAs($this->user)->get("/api/videos/{$video->id}/stream")->assertOk();

            $this->actingAs($this->user)
                ->withHeaders(['Range' => 'bytes=0-100'])
                ->get("/api/videos/{$video->id}/stream")
                ->assertStatus(206);

            $this->assertDatabaseCount('video_views', 1);
            $this->assertDatabaseHas('video_views', [
                'user_id' => $this->user->id,
                'video_id' => $video->id,
            ]);
        } finally {
            Storage::disk('public')->delete($path);
        }
    }

    public function test_search_ignores_seen_status_ordering(): void
    {
        $newerSeen = Video::factory()->create([
            'is_active' => true,
            'user_id' => $this->user->id,
            'title' => 'UniqueSearchableTitle Two',
            'created_at' => now(),
        ]);

        $olderUnseen = Video::factory()->create([
            'is_active' => true,
            'user_id' => $this->user->id,
            'title' => 'UniqueSearchableTitle One',
            'created_at' => now()->subHour(),
        ]);

        VideoView::factory()->create([
            'user_id' => $this->user->id,
            'video_id' => $newerSeen->id,
        ]);

        $response = $this->actingAs($this->user)
            ->getJson('/api/videos?search=UniqueSearchableTitle&mode=chronological');

        $response->assertOk()->assertJsonCount(2, 'data');
        $this->assertEquals(
            [$newerSeen->id, $olderUnseen->id],
            collect($response->json('data'))->pluck('id')->all()
        );
    }

    public function test_seen_history_resets_once_all_active_videos_have_been_seen(): void
    {
        $videos = Video::factory()->count(2)->create([
            'is_active' => true,
            'user_id' => $this->user->id,
        ]);

        foreach ($videos as $video) {
            VideoView::factory()->create([
                'user_id' => $this->user->id,
                'video_id' => $video->id,
            ]);
        }

        $response = $this->actingAs($this->user)->getJson('/api/videos');

        $response->assertOk()->assertJsonCount(2, 'data');
        $this->assertDatabaseCount('video_views', 0);
        collect($response->json('data'))->each(function ($video) {
            $this->assertFalse($video['is_seen_by_viewer']);
        });
    }

    public function test_seen_priority_is_scoped_per_user(): void
    {
        $otherUser = User::factory()->create();

        $newer = Video::factory()->create([
            'is_active' => true,
            'user_id' => $this->user->id,
            'created_at' => now(),
        ]);

        $older = Video::factory()->create([
            'is_active' => true,
            'user_id' => $this->user->id,
            'created_at' => now()->subHour(),
        ]);

        VideoView::factory()->create([
            'user_id' => $this->user->id,
            'video_id' => $newer->id,
        ]);

        $response = $this->actingAs($otherUser)->getJson('/api/videos?mode=chronological');

        $response->assertOk()->assertJsonCount(2, 'data');
        $this->assertEquals(
            [$newer->id, $older->id],
            collect($response->json('data'))->pluck('id')->all()
        );
        $this->assertFalse(collect($response->json('data'))->firstWhere('id', $newer->id)['is_seen_by_viewer']);
    }

    public function test_marking_video_as_viewed_records_it_as_seen(): void
    {
        $unseen = Video::factory()->create([
            'is_active' => true,
            'user_id' => $this->user->id,
            'created_at' => now()->subHour(),
        ]);

        $viewed = Video::factory()->create([
            'is_active' => true,
            'user_id' => $this->user->id,
            'created_at' => now(),
        ]);

        $response = $this->actingAs($this->user)->postJson("/api/videos/{$viewed->id}/view");

        $response->assertOk()->assertJson(['seen' => true]);
        $this->assertDatabaseHas('video_views', [
            'user_id' => $this->user->id,
            'video_id' => $viewed->id,
        ]);

        $feed = $this->actingAs($this->user)->getJson('/api/videos?mode=chronological');

        $feed->assertOk();
        $this->assertEquals(
            [$unseen->id, $viewed->id],
            collect($feed->json('data'))->pluck('id')->all()
        );
    }

    public function test_marking_video_as_viewed_is_idempotent(): void
    {
        $video = Video::factory()->create([
            'is_active' => true,
            'user_id' => $this->user->id,
        ]);

        $this->actingAs($this->user)->postJson("/api/videos/{$video->id}/view")->assertOk();
        $this->actingAs($this->user)->postJson("/api/videos/{$video->id}/view")->assertOk();

        $this->assertDatabaseCount('video_views', 1);
    }

    public function test_cannot_mark_inactive_video_as_viewed(): void
    {
        $video = Video::factory()->create([
            'is_active' => false,
            'user_id' => $this->user->id,
        ]);

        $response = $this->actingAs($this->user)->postJson("/api/videos/{$video->id}/view");

        $response->assertNotFound();
    }

    public function test_my_videos_return_public_storage_urls(): void
    {
        $video = Video::factory()->create([
            'is_active' => false,
            'user_id' => $this->user->id,
            'video_url' => 'videos/user_1_abc.mp4',
            'thumbnail_url' => 'thumbnails/thumb_1_abc.jpg',
        ]);

        $this->actingAs($this->user)
            ->getJson('/api/videos/mine')
            ->assertOk()
            ->assertJsonPath('data.0.video_url', '/storage/videos/user_1_abc.mp4')
            ->assertJsonPath('data.0.thumbnail_url', '/storage/thumbnails/thumb_1_abc.jpg');

        $this->actingAs($this->user)
            ->getJson("/api/videos/mine/{$video->id}")
            ->assertOk()
            ->assertJsonPath('video_url', '/storage/videos/user_1_abc.mp4')
            ->assertJsonPath('thumbnail_url', '/storage/thumbnails/thumb_1_abc.jpg');
    }

    public function test_my_videos_keep_null_thumbnail(): void
    {
        Video::factory()->create([
            'user_id' => $this->user->id,
            'video_url' => '/storage/videos/already.mp4',
            'thumbnail_url' => null,
        ]);

        $this->actingAs($this->user)
            ->getJson('/api/videos/mine')
            ->assertOk()
            ->assertJsonPath('data.0.video_url', '/storage/videos/already.mp4')
            ->assertJsonPath('data.0.thumbnail_url', null);
    }
}
