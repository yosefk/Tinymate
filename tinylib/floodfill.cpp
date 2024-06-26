#include <vector>
#include <algorithm>

struct FFillSegment
{
    unsigned short y;
    unsigned short l;
    unsigned short r;
    unsigned short prevl;
    unsigned short prevr;
    short dir;
};

enum
{
    UP = 1,
    DOWN = -1
};

#define ICV_PUSH( Y, L, R, PREV_L, PREV_R, DIR )  \
{                                                 \
    tail->y = (unsigned short)(Y);                        \
    tail->l = (unsigned short)(L);                        \
    tail->r = (unsigned short)(R);                        \
    tail->prevl = (unsigned short)(PREV_L);               \
    tail->prevr = (unsigned short)(PREV_R);               \
    tail->dir = (short)(DIR);                     \
    if( ++tail == buffer_end )                    \
    {                                             \
        buffer->resize(buffer->size() * 3/2);     \
        tail = &buffer->front() + (tail - head);  \
        head = &buffer->front();                  \
        buffer_end = head + buffer->size();       \
    }                                             \
}

#define ICV_POP( Y, L, R, PREV_L, PREV_R, DIR )   \
{                                                 \
    --tail;                                       \
    Y = tail->y;                                  \
    L = tail->l;                                  \
    R = tail->r;                                  \
    PREV_L = tail->prevl;                         \
    PREV_R = tail->prevr;                         \
    DIR = tail->dir;                              \
}


//the mask is a "native" numpy array so there's no stride of 4 between
//values like in images returned by pixels_alpha()
extern "C" void flood_fill_mask(unsigned char* mask, int mask_stride,
	       int width, int height, int seed_x, int seed_y, int mask_new_val,
	       int* region, int _8_connectivity) 
{
    std::vector<FFillSegment> buf;
    std::vector<FFillSegment>* buffer = &buf;
    size_t buffer_size = std::max( width, height ) * 2;
    buf.resize( buffer_size );

    unsigned char* img = mask + mask_stride*seed_y;
    int i, L, R;
    int XMin, XMax, YMin = seed_y, YMax = seed_y;
    FFillSegment* buffer_end = &buffer->front() + buffer->size(), *head = &buffer->front(), *tail = &buffer->front();

    L = R = XMin = XMax = seed_x;

    int val0 = img[L];
    img[L] = mask_new_val;

    while( ++R < width && img[R] == val0 ) {
        img[R] = mask_new_val;
    }

    while( --L >= 0 && img[L] == val0 ) {
        img[L] = mask_new_val;
    }

    XMax = --R;
    XMin = ++L;

    ICV_PUSH( seed_y, L, R, R + 1, R, UP );

    while( head != tail )
    {
        int k, YC, PL, PR, dir;
        ICV_POP( YC, L, R, PL, PR, dir );

        int data[][3] =
        {
            {-dir, L - _8_connectivity, R + _8_connectivity},
            {dir, L - _8_connectivity, PL - 1},
            {dir, PR + 1, R + _8_connectivity}
        };

        if( region )
        {
            if( XMax < R ) XMax = R;
            if( XMin > L ) XMin = L;
            if( YMax < YC ) YMax = YC;
            if( YMin > YC ) YMin = YC;
        }

        for( k = 0; k < 3; k++ )
        {
            dir = data[k][0];

            if( (unsigned)(YC + dir) >= (unsigned)height )
                continue;

            img = mask + mask_stride*(YC + dir);
            int left = data[k][1];
            int right = data[k][2];

            for( i = left; i <= right; i++ )
            {
                if( (unsigned)i < (unsigned)width && img[i] == val0 )
                {
                    int j = i;
                    img[i] = mask_new_val;
                    while( --j >= 0 && img[j] == val0 ) {
                        img[j] = mask_new_val;
		    }

                    while( ++i < width && img[i] == val0 ) {
                        img[i] = mask_new_val;
		    }

                    ICV_PUSH( YC + dir, j+1, i-1, L, R, -dir );
                }
            }
        }
    }

    if( region )
    {
        region[0] = XMin;
        region[1] = YMin;
        region[2] = XMax - XMin + 1;
        region[3] = YMax - YMin + 1;
    }
}

extern "C" void fill_color_based_on_mask(int* color, const unsigned char* mask,
		int color_stride, int mask_stride, int width, int height,
		const int* region, int new_color_value, int mask_value)
{
	int xstart = region[0];
	int ystart = region[1];
	int xend = xstart + region[2];
	int yend = ystart + region[3];

	for(int y=ystart; y<yend; ++y) {
		const unsigned char* mask_row = mask + mask_stride*y;
		int* color_row = color + (color_stride/4)*y;
		for(int x=xstart; x<xend; ++x) {
			if(mask_row[x] == mask_value) {
				color_row[x] = new_color_value;
			}
		}
	}
}

//mask is modified by this operation (the input is 1s where lines are and 0s where there aren't,
//and we fill some of the 0s with 2s; though we get "2" is the mask_new_val parameter.)
//the region we return is xmin, xmax, ymin, ymax [exclusive], differently
//from flood_fill_mask()
extern "C" void flood_fill_color_based_on_mask_many_seeds(int* color, unsigned char* mask,
		int color_stride, int mask_stride, int width, int height,
		int* region, int _8_connectivity,
		int mask_new_val, int new_color_value,
		const int* seed_x, const int* seed_y, int num_seeds)
{
	region[0] = region[1] = 1000000;
	region[2] = region[3] = -1;
	int fills = 0;
	for(int i=0; i<num_seeds; ++i) {
		int x = seed_x[i];
		int y = seed_y[i];
		if(x < 0 || y < 0 || x >= width || y >= height) {
			continue;
		}
		//don't fill regions already having the right color value;
		//don't fill inside the lines
		if(color[(color_stride/4)*y + x] == new_color_value || mask[mask_stride*y + x] == 1) {
			continue;
		}
		fills++;
		int curr_region[4];
		flood_fill_mask(mask, mask_stride, width, height, x, y, mask_new_val, curr_region, _8_connectivity);

		fill_color_based_on_mask(color, mask, color_stride, mask_stride, width, height, curr_region, new_color_value, mask_new_val);

		region[0] = std::min(region[0], curr_region[0]);
		region[1] = std::min(region[1], curr_region[1]);
		region[2] = std::max(region[2], curr_region[2]+curr_region[0]);
		region[3] = std::max(region[3], curr_region[3]+curr_region[1]);
	}
}


