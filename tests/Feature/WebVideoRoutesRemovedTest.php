<?php

namespace Tests\Feature;

use App\Models\User;
use App\Models\Video;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\UploadedFile;
use Illuminate\Support\Facades\Storage;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\TestCase;

class WebVideoRoutesRemovedTest extends TestCase
{
    use RefreshDatabase;

    private const FILE_CONTENTS = 'stored-file-contents';

    protected function setUp(): void
    {
        parent::setUp();

        Storage::fake('local');
        Storage::fake('public');
    }

    private function assertNoFilesStored(): void
    {
        $this->assertSame([], Storage::disk('local')->allFiles());
        $this->assertSame([], Storage::disk('public')->allFiles());
    }

    public function test_anonymous_cannot_view_upload_form(): void
    {
        $this->get('/upload-video')->assertNotFound();
    }

    public function test_anonymous_cannot_upload_video(): void
    {
        $response = $this->post('/upload-video', [
            'video' => UploadedFile::fake()->create('clip.mp4', 100, 'video/mp4'),
        ]);

        $response->assertNotFound();
        $this->assertNoFilesStored();
    }

    public function test_authenticated_user_cannot_use_web_upload(): void
    {
        $response = $this->actingAs(User::factory()->create())->post('/upload-video', [
            'video' => UploadedFile::fake()->create('clip.mp4', 100, 'video/mp4'),
        ]);

        $response->assertNotFound();
        $this->assertNoFilesStored();
    }

    public function test_anonymous_cannot_fetch_accepted_video(): void
    {
        Storage::disk('local')->put('accepted-videos/sample.mp4', self::FILE_CONTENTS);

        $this->get('/video/sample.mp4')->assertNotFound();
    }

    public function test_anonymous_cannot_fetch_pending_video(): void
    {
        Storage::disk('local')->put('pending-videos/batch/sample.mp4', self::FILE_CONTENTS);

        $this->get('/video/sample.mp4')->assertNotFound();
    }

    #[DataProvider('malformedFilenameProvider')]
    public function test_malformed_video_filenames_return_not_found(string $filename): void
    {
        Storage::disk('local')->put('accepted-videos/sample.mp4', self::FILE_CONTENTS);
        Storage::disk('local')->put('secret.txt', self::FILE_CONTENTS);

        $response = $this->get('/video/'.$filename);

        $response->assertNotFound();
        $this->assertStringNotContainsString(self::FILE_CONTENTS, (string) $response->getContent());
    }

    public static function malformedFilenameProvider(): array
    {
        return [
            'nested path' => ['nested/sample.mp4'],
            'dot segments' => ['../secret.txt'],
            'encoded separator' => ['nested%2Fsample.mp4'],
            'encoded dot segments' => ['%2E%2E%2Fsecret.txt'],
        ];
    }

    public function test_anonymous_cannot_upload_through_api(): void
    {
        $response = $this->postJson('/api/videos/upload', [
            'title' => 'Clip',
            'description' => 'Clip description',
            'video' => UploadedFile::fake()->create('clip.mp4', 100, 'video/mp4'),
        ]);

        $response->assertUnauthorized();
        $this->assertNoFilesStored();
    }

    public function test_authenticated_user_can_upload_through_api(): void
    {
        $user = User::factory()->create();

        $response = $this->actingAs($user)->postJson('/api/videos/upload', [
            'title' => 'Clip',
            'description' => 'Clip description',
            'video' => UploadedFile::fake()->create('clip.mp4', 100, 'video/mp4'),
        ]);

        $response->assertCreated();

        $video = Video::query()->where('user_id', $user->id)->sole();

        $this->assertFalse((bool) $video->is_active);
        Storage::disk('public')->assertExists($video->video_url);
    }
}
