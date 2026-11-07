#import <UIKit/UIKit.h>
#import "Foo.h"
#import "../util.h"

@implementation Bar
- (void)run {
    [Foo greet:1 with:2];
    [Foo build];
    [self local];
    [obj doIt];
    helper(3);
}
- (void)local {}
@end
